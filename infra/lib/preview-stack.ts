import * as path from "path";
import * as cdk from "aws-cdk-lib";
import * as acm from "aws-cdk-lib/aws-certificatemanager";
import * as cloudfront from "aws-cdk-lib/aws-cloudfront";
import * as cognito from "aws-cdk-lib/aws-cognito";
import * as origins from "aws-cdk-lib/aws-cloudfront-origins";
import * as s3 from "aws-cdk-lib/aws-s3";
import * as s3deploy from "aws-cdk-lib/aws-s3-deployment";
import { Construct } from "constructs";

export interface PreviewStackProps extends cdk.StackProps {
  /** Apex domain, for example acadadviser.com. Needs certificateArn too. */
  domain?: string;
  /** us-east-1 ACM certificate covering the domain and its www name. */
  certificateArn?: string;
}

const AUTH_DOMAIN_PREFIX = "acadadviser";

/** Static site on S3 and CloudFront, with visitor sign-in through Cognito. No servers, no student data. */
export class PreviewStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props: PreviewStackProps) {
    super(scope, id, props);

    if (Boolean(props.domain) !== Boolean(props.certificateArn)) {
      throw new Error("domain and certificateArn must be provided together");
    }

    const bucket = new s3.Bucket(this, "Site", {
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      encryption: s3.BucketEncryption.S3_MANAGED,
      enforceSSL: true,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
      autoDeleteObjects: true,
    });

    const userPool = new cognito.UserPool(this, "Users", {
      userPoolName: "acadadviser-users",
      featurePlan: cognito.FeaturePlan.ESSENTIALS,
      selfSignUpEnabled: true,
      signInAliases: { email: true },
      signInCaseSensitive: false,
      autoVerify: { email: true },
      accountRecovery: cognito.AccountRecovery.EMAIL_ONLY,
      passwordPolicy: { minLength: 10, requireSymbols: false },
      // Visitors' accounts must survive a stack replacement.
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });
    const authDomain = userPool.addDomain("Domain", {
      cognitoDomain: { domainPrefix: AUTH_DOMAIN_PREFIX },
      managedLoginVersion: cognito.ManagedLoginVersion.NEWER_MANAGED_LOGIN,
    });
    const authHost = `${AUTH_DOMAIN_PREFIX}.auth.${this.region}.amazoncognito.com`;

    const headers = new cloudfront.ResponseHeadersPolicy(this, "SecurityHeaders", {
      securityHeadersBehavior: {
        contentSecurityPolicy: {
          contentSecurityPolicy:
            `default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self' https://cognito-idp.${this.region}.amazonaws.com https://${authHost}; form-action 'none'; base-uri 'none'; frame-ancestors 'none'`,
          override: true,
        },
        contentTypeOptions: { override: true },
        frameOptions: { frameOption: cloudfront.HeadersFrameOption.DENY, override: true },
        referrerPolicy: {
          referrerPolicy: cloudfront.HeadersReferrerPolicy.STRICT_ORIGIN_WHEN_CROSS_ORIGIN,
          override: true,
        },
        strictTransportSecurity: {
          accessControlMaxAge: cdk.Duration.days(365),
          override: true,
        },
      },
    });

    const distribution = new cloudfront.Distribution(this, "Distribution", {
      comment: "AcadAdviserAI preview",
      defaultRootObject: "index.html",
      priceClass: cloudfront.PriceClass.PRICE_CLASS_100,
      domainNames: props.domain ? [props.domain, `www.${props.domain}`] : undefined,
      certificate: props.certificateArn
        ? acm.Certificate.fromCertificateArn(this, "Certificate", props.certificateArn)
        : undefined,
      defaultBehavior: {
        origin: origins.S3BucketOrigin.withOriginAccessControl(bucket),
        viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
        allowedMethods: cloudfront.AllowedMethods.ALLOW_GET_HEAD,
        cachePolicy: cloudfront.CachePolicy.CACHING_OPTIMIZED,
        responseHeadersPolicy: headers,
        compress: true,
      },
      // Without list permission, S3 answers 403 for a missing key.
      errorResponses: [
        { httpStatus: 403, responseHttpStatus: 404, responsePagePath: "/404.html", ttl: cdk.Duration.minutes(5) },
        { httpStatus: 404, responseHttpStatus: 404, responsePagePath: "/404.html", ttl: cdk.Duration.minutes(5) },
      ],
    });

    const siteOrigins = [
      `https://${distribution.distributionDomainName}`,
      ...(props.domain ? [`https://${props.domain}`, `https://www.${props.domain}`] : []),
    ];
    const client = userPool.addClient("Website", {
      generateSecret: false,
      preventUserExistenceErrors: true,
      // The site's own sign-in form uses the password flow; the hosted page uses SRP.
      authFlows: { userPassword: true, userSrp: true },
      supportedIdentityProviders: [cognito.UserPoolClientIdentityProvider.COGNITO],
      oAuth: {
        flows: { authorizationCodeGrant: true },
        scopes: [cognito.OAuthScope.OPENID, cognito.OAuthScope.EMAIL],
        callbackUrls: siteOrigins.map((origin) => `${origin}/account.html`),
        logoutUrls: siteOrigins.map((origin) => `${origin}/`),
      },
    });
    const branding = new cognito.CfnManagedLoginBranding(this, "LoginBranding", {
      userPoolId: userPool.userPoolId,
      clientId: client.userPoolClientId,
      useCognitoProvidedValues: true,
    });
    branding.node.addDependency(authDomain);

    new s3deploy.BucketDeployment(this, "Deploy", {
      sources: [
        s3deploy.Source.asset(path.join(__dirname, "../../build/preview")),
        s3deploy.Source.jsonData("static/auth-config.json", {
          clientId: client.userPoolClientId,
          region: this.region,
          // Browsers still holding the earlier auth.js redirect to the hosted page with this.
          domain: authHost,
        }),
      ],
      // File names never change, so browsers must revalidate or they mix old scripts with new config.
      cacheControl: [s3deploy.CacheControl.noCache()],
      destinationBucket: bucket,
      distribution,
      distributionPaths: ["/*"],
    });

    new cdk.CfnOutput(this, "SiteUrl", { value: `https://${distribution.distributionDomainName}` });
    new cdk.CfnOutput(this, "UserPoolId", { value: userPool.userPoolId });
    new cdk.CfnOutput(this, "DistributionDomain", { value: distribution.distributionDomainName });
  }
}
