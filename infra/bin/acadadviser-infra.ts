#!/usr/bin/env node
import "source-map-support/register";
import * as path from "path";
import * as cdk from "aws-cdk-lib";
import { AppStack } from "../lib/app-stack";

const app = new cdk.App();
const context = (key: string): string => {
  const value = app.node.tryGetContext(key);
  if (!value) throw new Error(`Set "${key}" in cdk.json or pass -c ${key}=...`);
  return value;
};

new AppStack(app, "AcadAdviserStack", {
  env: { account: process.env.CDK_DEFAULT_ACCOUNT, region: "us-east-1" },
  domain: context("domain"),
  certificateArn: context("certificateArn"),
  contactEmail: context("contactEmail"),
  reposRoot: path.resolve(__dirname, "../..", app.node.tryGetContext("reposRoot") ?? ".."),
  openaiSecretName: context("openaiSecretName"),
  cognitoFromEmail: app.node.tryGetContext("cognitoFromEmail") || undefined,
  monthlyBudgetUsd: Number(context("monthlyBudgetUsd")),
  gatewaySettings: app.node.tryGetContext("gatewaySettings") ?? {},
});
