#!/usr/bin/env node
import "source-map-support/register";
import * as cdk from "aws-cdk-lib";
import { PreviewStack } from "../lib/preview-stack";

const app = new cdk.App();

new PreviewStack(app, "AcadAdviserPreviewStack", {
  env: { account: process.env.CDK_DEFAULT_ACCOUNT, region: "us-east-1" },
  domain: app.node.tryGetContext("domain"),
  certificateArn: app.node.tryGetContext("certificateArn"),
});
