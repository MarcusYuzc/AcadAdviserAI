import { execSync } from "child_process";
import * as fs from "fs";
import * as path from "path";
import * as cdk from "aws-cdk-lib";
import { CfnOutput, CustomResource, Duration, RemovalPolicy, SecretValue, Tags } from "aws-cdk-lib";
import * as acm from "aws-cdk-lib/aws-certificatemanager";
import * as budgets from "aws-cdk-lib/aws-budgets";
import * as cloudwatch from "aws-cdk-lib/aws-cloudwatch";
import * as cw_actions from "aws-cdk-lib/aws-cloudwatch-actions";
import * as cognito from "aws-cdk-lib/aws-cognito";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import * as ecr_assets from "aws-cdk-lib/aws-ecr-assets";
import * as ecs from "aws-cdk-lib/aws-ecs";
import * as ecs_patterns from "aws-cdk-lib/aws-ecs-patterns";
import * as efs from "aws-cdk-lib/aws-efs";
import * as elbv2 from "aws-cdk-lib/aws-elasticloadbalancingv2";
import * as lambda from "aws-cdk-lib/aws-lambda";
import * as logs from "aws-cdk-lib/aws-logs";
import * as rds from "aws-cdk-lib/aws-rds";
import * as secretsmanager from "aws-cdk-lib/aws-secretsmanager";
import * as servicediscovery from "aws-cdk-lib/aws-servicediscovery";
import * as ses from "aws-cdk-lib/aws-ses";
import * as sns from "aws-cdk-lib/aws-sns";
import * as subscriptions from "aws-cdk-lib/aws-sns-subscriptions";
import * as cr from "aws-cdk-lib/custom-resources";
import { Construct } from "constructs";

export interface AppStackProps extends cdk.StackProps {
  /** Apex domain, for example acadadviser.com. */
  domain: string;
  /** ACM certificate in this region covering the domain and its www name. */
  certificateArn: string;
  /** Shown on the site; also receives alarms and budget alerts. */
  contactEmail: string;
  /** Folder that holds this repo and its siblings ProgramSearchAgent, StudentNormalizer, SchoolInfoProcessor. */
  reposRoot: string;
  /** Existing Secrets Manager secret whose JSON field OPENAI_API_KEY holds the key. */
  openaiSecretName: string;
  /** Send sign-in email through SES from this address. Set only after the domain's DKIM records verify. */
  cognitoFromEmail?: string;
  /** Monthly AWS cost that triggers a budget email. */
  monthlyBudgetUsd: number;
  /** Extra gateway settings, for example {"MODEL_RUNS_PER_DAY": "200"}. */
  gatewaySettings?: Record<string, string>;
}

const NAMESPACE = "acadadviser.internal";
const ARM64 = {
  cpuArchitecture: ecs.CpuArchitecture.ARM64,
  operatingSystemFamily: ecs.OperatingSystemFamily.LINUX,
};

/**
 * The whole site: the public gateway behind an HTTPS load balancer, and PSA, StudentNormalizer and
 * SchoolInfoProcessor as private services that only their callers can reach. One RDS instance holds a
 * database per service; PSA keeps its files on EFS.
 */
export class AppStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props: AppStackProps) {
    super(scope, id, props);
    Tags.of(this).add("Project", "AcadAdviserAI");

    const vpc = new ec2.Vpc(this, "Vpc", {
      maxAzs: 2,
      natGateways: 1,
      subnetConfiguration: [
        { name: "Public", subnetType: ec2.SubnetType.PUBLIC, cidrMask: 24 },
        { name: "Private", subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS, cidrMask: 24 },
      ],
    });
    // Image layers come from S3; this keeps them off the NAT gateway's data charge.
    vpc.addGatewayEndpoint("S3", { service: ec2.GatewayVpcEndpointAwsService.S3 });
    const privateSubnets = { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS };

    // Database: student profiles, accounts and plan records.

    const database = new rds.DatabaseInstance(this, "Database", {
      engine: rds.DatabaseInstanceEngine.postgres({ version: rds.PostgresEngineVersion.VER_16 }),
      instanceType: ec2.InstanceType.of(ec2.InstanceClass.T4G, ec2.InstanceSize.MICRO),
      credentials: rds.Credentials.fromGeneratedSecret("aa_admin"),
      vpc,
      vpcSubnets: privateSubnets,
      allocatedStorage: 20,
      maxAllocatedStorage: 100,
      storageEncrypted: true,
      publiclyAccessible: false,
      multiAz: false,
      backupRetention: Duration.days(7),
      deleteAutomatedBackups: false,
      // Holds customer and payment records: a destroy keeps a final snapshot, and must first turn this off.
      deletionProtection: true,
      removalPolicy: RemovalPolicy.SNAPSHOT,
      autoMinorVersionUpgrade: true,
    });

    const appDbSecret = (secretId: string, username: string, dbname: string) =>
      new secretsmanager.Secret(this, secretId, {
        description: `${username} login for the ${dbname} database`,
        generateSecretString: {
          secretStringTemplate: this.toJsonString({
            username,
            dbname,
            host: database.dbInstanceEndpointAddress,
            port: 5432,
            engine: "postgres",
          }),
          generateStringKey: "password",
          excludePunctuation: true,
          passwordLength: 32,
        },
      });
    const gatewayDb = appDbSecret("GatewayDbSecret", "gateway_app", "acadadviser");
    const snDb = appDbSecret("SnDbSecret", "sn_app", "student_normalizer");
    const sipDb = appDbSecret("SipDbSecret", "sip_app", "school_info");
    const appDbSecrets = [gatewayDb, snDb, sipDb];

    const shortLogs = (logId: string) =>
      new logs.LogGroup(this, logId, { retention: logs.RetentionDays.TWO_WEEKS, removalPolicy: RemovalPolicy.DESTROY });
    const provisionSg = new ec2.SecurityGroup(this, "ProvisionDbSg", { vpc, description: "Database provisioning Lambda" });
    // A stack-level rule, so the provisioning step can wait for it without waiting on every service's rule.
    const provisionIngress = new ec2.CfnSecurityGroupIngress(this, "ProvisionDbToDatabase", {
      groupId: database.connections.securityGroups[0].securityGroupId,
      sourceSecurityGroupId: provisionSg.securityGroupId,
      ipProtocol: "tcp",
      fromPort: 5432,
      toPort: 5432,
      description: "Provisioning Lambda to RDS",
    });
    const provisionFn = new lambda.Function(this, "ProvisionDbFn", {
      runtime: lambda.Runtime.PYTHON_3_12,
      architecture: lambda.Architecture.X86_64,
      handler: "index.handler",
      timeout: Duration.minutes(2),
      vpc,
      vpcSubnets: privateSubnets,
      securityGroups: [provisionSg],
      logGroup: shortLogs("ProvisionDbFnLogs"),
      environment: {
        ADMIN_SECRET_ARN: database.secret!.secretArn,
        APP_SECRET_ARNS: appDbSecrets.map((secret) => secret.secretArn).join(","),
      },
      code: lambda.Code.fromAsset(path.join(__dirname, "../provision-db"), {
        bundling: {
          image: lambda.Runtime.PYTHON_3_12.bundlingImage,
          local: {
            tryBundle(outputDir: string): boolean {
              const src = path.join(__dirname, "../provision-db");
              execSync(
                "python3 -m pip install --disable-pip-version-check --platform manylinux2014_x86_64 " +
                  `--implementation cp --python-version 3.12 --only-binary=:all: -r "${src}/requirements.txt" -t "${outputDir}"`,
                { stdio: "inherit" },
              );
              fs.copyFileSync(path.join(src, "index.py"), path.join(outputDir, "index.py"));
              return true;
            },
          },
          command: ["bash", "-c", "pip install -r requirements.txt -t /asset-output && cp index.py /asset-output"],
        },
      }),
    });
    database.secret!.grantRead(provisionFn);
    appDbSecrets.forEach((secret) => secret.grantRead(provisionFn));
    const provisioned = new CustomResource(this, "Databases", {
      serviceToken: new cr.Provider(this, "ProvisionDbProvider", {
        onEventHandler: provisionFn,
        logGroup: shortLogs("ProvisionDbProviderLogs"),
      }).serviceToken,
      // A new or replaced app secret re-runs the provisioning.
      properties: { appSecrets: appDbSecrets.map((secret) => secret.secretArn).join(",") },
    });
    provisioned.node.addDependency(database.node.defaultChild!, provisionIngress);
    // Only the ECS service resource waits, not its security group, which the database's rules refer to.
    const afterDatabases = (service: ecs.BaseService) =>
      (service.node.defaultChild as cdk.CfnResource).node.addDependency(provisioned);

    // Secrets supplied by the owner.

    const openaiSecret = secretsmanager.Secret.fromSecretNameV2(this, "OpenAiSecret", props.openaiSecretName);
    const openaiKey = ecs.Secret.fromSecretsManager(openaiSecret, "OPENAI_API_KEY");
    // Empty values keep billing off ("Subscriptions are not open yet") until the owner fills them in.
    const stripeSecret = new secretsmanager.Secret(this, "StripeSecret", {
      description: "Stripe keys for the AcadAdviserAI gateway",
      secretObjectValue: {
        STRIPE_SECRET_KEY: SecretValue.unsafePlainText(""),
        STRIPE_WEBHOOK_SECRET: SecretValue.unsafePlainText(""),
        STRIPE_PRICE_ID: SecretValue.unsafePlainText(""),
      },
    });

    // Sign-in.

    const userPool = new cognito.UserPool(this, "Users", {
      userPoolName: "acadadviser-users",
      featurePlan: cognito.FeaturePlan.ESSENTIALS,
      selfSignUpEnabled: true,
      signInAliases: { email: true },
      signInCaseSensitive: false,
      autoVerify: { email: true },
      accountRecovery: cognito.AccountRecovery.EMAIL_ONLY,
      passwordPolicy: { minLength: 10, requireSymbols: false },
      email: props.cognitoFromEmail
        ? cognito.UserPoolEmail.withSES({
            fromEmail: props.cognitoFromEmail,
            fromName: "AcadAdviserAI",
            sesRegion: this.region,
            sesVerifiedDomain: props.domain,
          })
        : undefined,
      // Accounts are tied to subscriptions, so they survive a stack replacement.
      removalPolicy: RemovalPolicy.RETAIN,
      deletionProtection: true,
    });
    const userPoolClient = userPool.addClient("Website", {
      generateSecret: false,
      preventUserExistenceErrors: true,
      // The site's own form signs in with the password flow and hands the ID token to the gateway.
      authFlows: { userPassword: true },
      idTokenValidity: Duration.hours(1),
    });
    // DKIM for the domain, so Cognito can send from it through SES. Google Workspace mail is unaffected.
    const mailIdentity = new ses.EmailIdentity(this, "MailDomain", {
      identity: ses.Identity.domain(props.domain),
    });

    // Services.

    const cluster = new ecs.Cluster(this, "Cluster", { vpc, clusterName: "acadadviser" });
    cluster.addDefaultCloudMapNamespace({ name: NAMESPACE, type: servicediscovery.NamespaceType.DNS_PRIVATE, vpc });
    const internalUrl = (name: string, port: number) => `http://${name}.${NAMESPACE}:${port}`;

    const repo = (name: string) => path.join(props.reposRoot, name);
    const image = (assetId: string, directory: string, target?: string) =>
      ecs.ContainerImage.fromDockerImageAsset(
        new ecr_assets.DockerImageAsset(this, assetId, {
          directory,
          target,
          platform: ecr_assets.Platform.LINUX_ARM64,
        }),
      );
    const logDriver = (name: string) =>
      ecs.LogDrivers.awsLogs({
        streamPrefix: name,
        logGroup: new logs.LogGroup(this, `${name}Logs`, {
          logGroupName: `/acadadviser/${name}`,
          retention: logs.RetentionDays.ONE_MONTH,
          removalPolicy: RemovalPolicy.DESTROY,
        }),
      });

    interface PrivateServiceOptions {
      image: ecs.ContainerImage;
      port: number;
      cpu: number;
      memoryLimitMiB: number;
      environment: Record<string, string>;
      secrets: Record<string, ecs.Secret>;
      healthCommand: string[];
      startPeriod: Duration;
      /** PSA keeps runs in memory and files on EFS, so it stops the old task before starting a new one. */
      replaceInPlace?: boolean;
    }
    const privateService = (name: string, options: PrivateServiceOptions) => {
      const taskDefinition = new ecs.FargateTaskDefinition(this, `${name}Task`, {
        cpu: options.cpu,
        memoryLimitMiB: options.memoryLimitMiB,
        runtimePlatform: ARM64,
      });
      const container = taskDefinition.addContainer(name, {
        image: options.image,
        logging: logDriver(name),
        environment: options.environment,
        secrets: options.secrets,
        portMappings: [{ containerPort: options.port }],
        healthCheck: {
          command: ["CMD", ...options.healthCommand],
          interval: Duration.seconds(30),
          timeout: Duration.seconds(5),
          retries: 3,
          startPeriod: options.startPeriod,
        },
      });
      const service = new ecs.FargateService(this, `${name}Service`, {
        cluster,
        taskDefinition,
        serviceName: `acadadviser-${name}`,
        desiredCount: 1,
        vpcSubnets: privateSubnets,
        assignPublicIp: false,
        cloudMapOptions: { name, dnsRecordType: servicediscovery.DnsRecordType.A, dnsTtl: Duration.seconds(10) },
        circuitBreaker: { rollback: true },
        minHealthyPercent: options.replaceInPlace ? 0 : 100,
        maxHealthyPercent: options.replaceInPlace ? 100 : 200,
      });
      afterDatabases(service);
      return { service, taskDefinition, container };
    };
    const curlHealth = (port: number) => ["curl", "-fsS", `http://127.0.0.1:${port}/health`];

    const sn = privateService("sn", {
      image: image("SnImage", repo("StudentNormalizer")),
      port: 8000,
      cpu: 512,
      memoryLimitMiB: 1024,
      environment: {
        DB_NAME: "student_normalizer",
        DB_USER: "sn_app",
        DB_PORT: "5432",
        RUN_MIGRATIONS: "true",
        OPENAI_MODEL: "gpt-4o-mini",
        // If usnews.com refuses AWS addresses, SN falls back to its built-in 2026 ranking list.
        US_NEWS_LIVE_ENABLED: "true",
        WIKIDATA_ENABLED: "true",
        // Wikimedia asks API clients to identify themselves with a contact.
        WIKIDATA_USER_AGENT: `AcadAdviserAI/1.0 (https://${props.domain}; ${props.contactEmail})`,
      },
      secrets: {
        DB_HOST: ecs.Secret.fromSecretsManager(snDb, "host"),
        DB_PASSWORD: ecs.Secret.fromSecretsManager(snDb, "password"),
        OPENAI_API_KEY: openaiKey,
      },
      healthCommand: curlHealth(8000),
      startPeriod: Duration.seconds(90),
    });

    const sip = privateService("sip", {
      image: image("SipImage", repo("SchoolInfoProcessor")),
      port: 8000,
      cpu: 512,
      memoryLimitMiB: 1024,
      environment: {
        DB_NAME: "school_info",
        DB_USER: "sip_app",
        DB_PORT: "5432",
        RUN_MIGRATIONS: "true",
        SIP_MODE: "api",
        // Loads the catalog baked into the image on each start.
        SIP_LOAD_CATALOG: "true",
      },
      secrets: {
        DB_HOST: ecs.Secret.fromSecretsManager(sipDb, "host"),
        DB_PASSWORD: ecs.Secret.fromSecretsManager(sipDb, "password"),
      },
      healthCommand: curlHealth(8000),
      startPeriod: Duration.seconds(300),
    });

    const psaFiles = new efs.FileSystem(this, "PsaFiles", {
      vpc,
      vpcSubnets: privateSubnets,
      encrypted: true,
      throughputMode: efs.ThroughputMode.ELASTIC,
      enableAutomaticBackups: true,
      // Conversations and uploaded profiles: kept if the stack is destroyed.
      removalPolicy: RemovalPolicy.RETAIN,
    });
    const psaAccess = psaFiles.addAccessPoint("PsaAccess", {
      path: "/psa",
      createAcl: { ownerUid: "10001", ownerGid: "10001", permissions: "750" },
      posixUser: { uid: "10001", gid: "10001" },
    });
    const psa = privateService("psa", {
      image: image("PsaImage", repo("ProgramSearchAgent"), "runtime"),
      port: 8003,
      cpu: 512,
      memoryLimitMiB: 1024,
      environment: {
        OPENAI_AUTH_MODE: "api_key",
        STUDENT_NORMALIZER_URL: internalUrl("sn", 8000),
        SCHOOL_INFO_PROCESSOR_URL: internalUrl("sip", 8000),
        PROGRAM_SEARCH_AGENT_DATA_DIR: "/data",
        TELEMETRY_DIR: "/data/telemetry",
      },
      secrets: { OPENAI_API_KEY: openaiKey },
      healthCommand: [
        "python", "-c",
        "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8003/health', timeout=2)",
      ],
      startPeriod: Duration.seconds(30),
      replaceInPlace: true,
    });
    psa.taskDefinition.addVolume({
      name: "data",
      efsVolumeConfiguration: {
        fileSystemId: psaFiles.fileSystemId,
        transitEncryption: "ENABLED",
        authorizationConfig: { accessPointId: psaAccess.accessPointId, iam: "ENABLED" },
      },
    });
    psa.container.addMountPoints({ containerPath: "/data", sourceVolume: "data", readOnly: false });
    psaFiles.grantReadWrite(psa.taskDefinition.taskRole);
    psaFiles.connections.allowDefaultPortFrom(psa.service);

    const gateway = new ecs_patterns.ApplicationLoadBalancedFargateService(this, "Gateway", {
      cluster,
      serviceName: "acadadviser-gateway",
      cpu: 256,
      memoryLimitMiB: 512,
      desiredCount: 1,
      minHealthyPercent: 100,
      maxHealthyPercent: 200,
      runtimePlatform: ARM64,
      publicLoadBalancer: true,
      assignPublicIp: false,
      taskSubnets: privateSubnets,
      protocol: elbv2.ApplicationProtocol.HTTPS,
      certificate: acm.Certificate.fromCertificateArn(this, "Certificate", props.certificateArn),
      sslPolicy: elbv2.SslPolicy.RECOMMENDED_TLS,
      redirectHTTP: true,
      // Creating a search can take minutes with no bytes sent; event streams send pings.
      idleTimeout: Duration.seconds(360),
      circuitBreaker: { rollback: true },
      healthCheckGracePeriod: Duration.seconds(60),
      taskImageOptions: {
        image: image("GatewayImage", path.join(__dirname, "../..")),
        containerPort: 8080,
        logDriver: logDriver("gateway"),
        environment: {
          SITE_DOMAIN: props.domain,
          CONTACT_EMAIL: props.contactEmail,
          COOKIE_SECURE: "true",
          PSA_URL: internalUrl("psa", 8003),
          SIP_URL: internalUrl("sip", 8000),
          DB_NAME: "acadadviser",
          DB_USER: "gateway_app",
          DB_PORT: "5432",
          // Only the load balancer can reach the task, so its X-Forwarded-For is trusted.
          TRUSTED_PROXY_CIDRS: vpc.vpcCidrBlock,
          COGNITO_REGION: this.region,
          COGNITO_USER_POOL_ID: userPool.userPoolId,
          COGNITO_CLIENT_ID: userPoolClient.userPoolClientId,
          ...props.gatewaySettings,
        },
        secrets: {
          DB_HOST: ecs.Secret.fromSecretsManager(gatewayDb, "host"),
          DB_PASSWORD: ecs.Secret.fromSecretsManager(gatewayDb, "password"),
          STRIPE_SECRET_KEY: ecs.Secret.fromSecretsManager(stripeSecret, "STRIPE_SECRET_KEY"),
          STRIPE_WEBHOOK_SECRET: ecs.Secret.fromSecretsManager(stripeSecret, "STRIPE_WEBHOOK_SECRET"),
          STRIPE_PRICE_ID: ecs.Secret.fromSecretsManager(stripeSecret, "STRIPE_PRICE_ID"),
        },
      },
    });
    afterDatabases(gateway.service);
    gateway.targetGroup.configureHealthCheck({
      path: "/health",
      healthyHttpCodes: "200",
      interval: Duration.seconds(30),
      timeout: Duration.seconds(5),
      healthyThresholdCount: 2,
      unhealthyThresholdCount: 3,
    });
    gateway.targetGroup.setAttribute("deregistration_delay.timeout_seconds", "30");

    // Who may call whom. Nothing but the gateway is reachable from the load balancer.
    psa.service.connections.allowFrom(gateway.service, ec2.Port.tcp(8003), "gateway to PSA");
    sip.service.connections.allowFrom(gateway.service, ec2.Port.tcp(8000), "gateway to SIP");
    sip.service.connections.allowFrom(psa.service, ec2.Port.tcp(8000), "PSA to SIP");
    sn.service.connections.allowFrom(psa.service, ec2.Port.tcp(8000), "PSA to SN");
    for (const service of [gateway.service, sn.service, sip.service]) {
      database.connections.allowDefaultPortFrom(service);
    }

    // Alerts.

    const alerts = new sns.Topic(this, "Alerts", { displayName: "AcadAdviserAI alerts" });
    alerts.addSubscription(new subscriptions.EmailSubscription(props.contactEmail));
    const alarm = (alarmId: string, metric: cloudwatch.IMetric, threshold: number, description: string, periods = 1) =>
      new cloudwatch.Alarm(this, alarmId, {
        metric,
        threshold,
        evaluationPeriods: periods,
        comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
        treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
        alarmDescription: description,
      }).addAlarmAction(new cw_actions.SnsAction(alerts));
    const fiveMinutes = { period: Duration.minutes(5), statistic: "Sum" };
    alarm(
      "GatewayErrors",
      gateway.loadBalancer.metrics.httpCodeTarget(elbv2.HttpCodeTarget.TARGET_5XX_COUNT, fiveMinutes),
      5,
      "The gateway answered 5 or more server errors in 5 minutes.",
    );
    alarm(
      "SiteDown",
      gateway.loadBalancer.metrics.httpCodeElb(elbv2.HttpCodeElb.ELB_5XX_COUNT, fiveMinutes),
      5,
      "The load balancer could not reach the gateway 5 or more times in 5 minutes.",
    );
    alarm(
      "GatewayUnhealthy",
      gateway.targetGroup.metrics.unhealthyHostCount({ period: Duration.minutes(1), statistic: "Maximum" }),
      1,
      "The gateway has failed its health check for 3 minutes.",
      3,
    );
    new budgets.CfnBudget(this, "MonthlyBudget", {
      budget: {
        budgetName: "acadadviser-monthly",
        budgetType: "COST",
        timeUnit: "MONTHLY",
        budgetLimit: { amount: props.monthlyBudgetUsd, unit: "USD" },
      },
      notificationsWithSubscribers: [
        { type: "ACTUAL", threshold: 80 },
        { type: "FORECASTED", threshold: 100 },
      ].map(({ type, threshold }) => ({
        notification: { notificationType: type, comparisonOperator: "GREATER_THAN", threshold, thresholdType: "PERCENTAGE" },
        subscribers: [{ subscriptionType: "EMAIL", address: props.contactEmail }],
      })),
    });

    // What the owner needs after a deploy.

    new CfnOutput(this, "LoadBalancerDns", {
      value: gateway.loadBalancer.loadBalancerDnsName,
      description: `At Namecheap, point ALIAS @ and CNAME www for ${props.domain} here`,
    });
    new CfnOutput(this, "StripeWebhookUrl", { value: `https://${props.domain}/api/stripe/webhook` });
    new CfnOutput(this, "StripeSecretName", {
      value: stripeSecret.secretName,
      description: "Fill STRIPE_SECRET_KEY, STRIPE_WEBHOOK_SECRET and STRIPE_PRICE_ID, then redeploy the gateway",
    });
    new CfnOutput(this, "UserPoolId", { value: userPool.userPoolId });
    new CfnOutput(this, "UserPoolClientId", { value: userPoolClient.userPoolClientId });
    mailIdentity.dkimRecords.forEach((record, index) => {
      new CfnOutput(this, `DkimRecord${index + 1}`, {
        value: cdk.Fn.join(" ", ["CNAME", record.name, record.value]),
        description: "Add at Namecheap so SES can send sign-in email from the domain",
      });
    });
  }
}
