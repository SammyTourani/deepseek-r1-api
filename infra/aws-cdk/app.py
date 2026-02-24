#!/usr/bin/env python3
"""
DeepSeek R1 API — AWS CDK Infrastructure
=========================================
Defines:
  - HTTP API Gateway (v2) with API key auth
  - Lambda proxy (round-robin load balancing to vLLM nodes)
  - Lambda scaler (CloudWatch alarm-triggered auto-scaling)
  - DynamoDB table for node pool state
  - CloudWatch alarms (latency, error rate)
  - Secrets Manager (SF Compute key + HF token)
  - IAM roles and policies
"""
import os
import aws_cdk as cdk
from aws_cdk import (
    Stack,
    Duration,
    RemovalPolicy,
    aws_apigatewayv2 as apigwv2,
    aws_apigatewayv2_integrations as apigwv2_integrations,
    aws_lambda as lambda_,
    aws_dynamodb as dynamodb,
    aws_cloudwatch as cloudwatch,
    aws_cloudwatch_actions as cw_actions,
    aws_iam as iam,
    aws_secretsmanager as secretsmanager,
    aws_logs as logs,
    aws_sns as sns,
    aws_sns_subscriptions as subs,
)
from constructs import Construct


class DeepSeekApiStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # ─── Secrets Manager ─────────────────────────────────────────────────
        sf_secret = secretsmanager.Secret(
            self,
            "SFComputeSecret",
            secret_name="deepseek-r1/sfcompute-api-key",
            description="SF Compute API key for auto-scaling",
            generate_secret_string=secretsmanager.SecretStringGenerator(
                secret_string_template='{"api_key": "REPLACE_ME"}',
                generate_string_key="unused",
            ),
        )

        hf_secret = secretsmanager.Secret(
            self,
            "HuggingFaceSecret",
            secret_name="deepseek-r1/hf-token",
            description="HuggingFace token for model download",
            generate_secret_string=secretsmanager.SecretStringGenerator(
                secret_string_template='{"token": "REPLACE_ME"}',
                generate_string_key="unused",
            ),
        )

        client_api_key_secret = secretsmanager.Secret(
            self,
            "ClientApiKeySecret",
            secret_name="deepseek-r1/client-api-key",
            description="API key clients must pass in x-api-key header",
        )

        # ─── DynamoDB — node pool state ───────────────────────────────────────
        node_table = dynamodb.Table(
            self,
            "NodeTable",
            table_name="deepseek-r1-nodes",
            partition_key=dynamodb.Attribute(
                name="node_id", type=dynamodb.AttributeType.STRING
            ),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.RETAIN,
            time_to_live_attribute="ttl",
            point_in_time_recovery=True,
        )

        # GSI: query by status (healthy/unhealthy)
        node_table.add_global_secondary_index(
            index_name="StatusIndex",
            partition_key=dynamodb.Attribute(
                name="status", type=dynamodb.AttributeType.STRING
            ),
            sort_key=dynamodb.Attribute(
                name="last_checked", type=dynamodb.AttributeType.STRING
            ),
            projection_type=dynamodb.ProjectionType.ALL,
        )

        # ─── Lambda execution role ────────────────────────────────────────────
        lambda_role = iam.Role(
            self,
            "LambdaRole",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "service-role/AWSLambdaBasicExecutionRole"
                ),
            ],
        )
        node_table.grant_read_write_data(lambda_role)
        sf_secret.grant_read(lambda_role)
        hf_secret.grant_read(lambda_role)
        client_api_key_secret.grant_read(lambda_role)

        # Allow Lambda to publish CloudWatch metrics
        lambda_role.add_to_policy(
            iam.PolicyStatement(
                actions=["cloudwatch:PutMetricData"],
                resources=["*"],
            )
        )

        # ─── Lambda: Proxy ────────────────────────────────────────────────────
        proxy_lambda = lambda_.Function(
            self,
            "ProxyLambda",
            function_name="deepseek-r1-proxy",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="proxy.handler",
            code=lambda_.Code.from_asset("lambda"),
            role=lambda_role,
            timeout=Duration.seconds(120),  # long generation
            memory_size=512,
            environment={
                "NODE_TABLE_NAME": node_table.table_name,
                "CLIENT_API_KEY_SECRET": client_api_key_secret.secret_name,
                "VLLM_PORT": "8000",
                "LOG_LEVEL": "INFO",
            },
            log_retention=logs.RetentionDays.ONE_WEEK,
        )

        # ─── Lambda: Scaler ───────────────────────────────────────────────────
        scaler_lambda = lambda_.Function(
            self,
            "ScalerLambda",
            function_name="deepseek-r1-scaler",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="scaler.handler",
            code=lambda_.Code.from_asset("lambda"),
            role=lambda_role,
            timeout=Duration.minutes(10),
            memory_size=256,
            environment={
                "NODE_TABLE_NAME": node_table.table_name,
                "SF_SECRET_NAME": sf_secret.secret_name,
                "HF_SECRET_NAME": hf_secret.secret_name,
                "MIN_NODES": "1",
                "MAX_NODES": "4",
                "SF_ZONE": "landsend",
                "INSTANCE_TYPE": "h100-80gb-2x",
                "MODEL_NAME": "deepseek-ai/DeepSeek-R1-Distill-Llama-70B",
                "TENSOR_PARALLEL_SIZE": "2",
                "VLLM_PORT": "8000",
            },
            log_retention=logs.RetentionDays.ONE_WEEK,
        )

        # ─── HTTP API Gateway v2 ──────────────────────────────────────────────
        http_api = apigwv2.HttpApi(
            self,
            "DeepSeekHttpApi",
            api_name="deepseek-r1-api",
            description="DeepSeek R1 OpenAI-compatible inference API",
            cors_preflight=apigwv2.CorsPreflightOptions(
                allow_origins=["*"],
                allow_methods=[
                    apigwv2.CorsHttpMethod.POST,
                    apigwv2.CorsHttpMethod.GET,
                    apigwv2.CorsHttpMethod.OPTIONS,
                ],
                allow_headers=["Content-Type", "x-api-key", "Authorization"],
                max_age=Duration.days(1),
            ),
        )

        proxy_integration = apigwv2_integrations.HttpLambdaIntegration(
            "ProxyIntegration",
            proxy_lambda,
            payload_format_version=apigwv2.PayloadFormatVersion.VERSION_2_0,
        )

        # Route all /v1/* traffic through proxy Lambda
        http_api.add_routes(
            path="/v1/{proxy+}",
            methods=[
                apigwv2.HttpMethod.POST,
                apigwv2.HttpMethod.GET,
                apigwv2.HttpMethod.DELETE,
            ],
            integration=proxy_integration,
        )
        http_api.add_routes(
            path="/health",
            methods=[apigwv2.HttpMethod.GET],
            integration=proxy_integration,
        )

        # ─── CloudWatch Alarms ────────────────────────────────────────────────
        # Alarm: proxy Lambda p99 duration > 2000ms for 3 minutes
        high_latency_alarm = cloudwatch.Alarm(
            self,
            "HighLatencyAlarm",
            alarm_name="deepseek-r1-high-latency",
            alarm_description="vLLM inference latency >2s (scale up trigger)",
            metric=proxy_lambda.metric_duration(
                statistic="p99",
                period=Duration.minutes(1),
            ),
            threshold=2000,
            evaluation_periods=3,
            comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
        )

        # Alarm: error rate > 5% for 2 minutes
        error_alarm = cloudwatch.Alarm(
            self,
            "HighErrorRateAlarm",
            alarm_name="deepseek-r1-high-errors",
            alarm_description="Lambda error rate >5%",
            metric=proxy_lambda.metric_errors(period=Duration.minutes(1)),
            threshold=5,
            evaluation_periods=2,
            comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
        )

        # Scale-down alarm: low latency for 10 minutes
        low_latency_alarm = cloudwatch.Alarm(
            self,
            "LowLatencyAlarm",
            alarm_name="deepseek-r1-low-latency",
            alarm_description="vLLM inference latency <500ms (scale down trigger)",
            metric=proxy_lambda.metric_duration(
                statistic="p99",
                period=Duration.minutes(1),
            ),
            threshold=500,
            evaluation_periods=10,
            comparison_operator=cloudwatch.ComparisonOperator.LESS_THAN_THRESHOLD,
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
        )

        # Wire alarms → scaler Lambda via SNS
        alarm_topic = sns.Topic(
            self, "ScalingAlarmTopic", topic_name="deepseek-r1-scaling-alarms"
        )
        alarm_topic.add_subscription(
            subs.LambdaSubscription(scaler_lambda)
        )

        high_latency_alarm.add_alarm_action(cw_actions.SnsAction(alarm_topic))
        low_latency_alarm.add_alarm_action(cw_actions.SnsAction(alarm_topic))

        # ─── Outputs ──────────────────────────────────────────────────────────
        cdk.CfnOutput(
            self,
            "ApiEndpoint",
            value=http_api.api_endpoint,
            description="DeepSeek R1 API endpoint (set in Cloudflare DNS)",
            export_name="DeepSeekR1ApiEndpoint",
        )
        cdk.CfnOutput(
            self,
            "NodeTableName",
            value=node_table.table_name,
            export_name="DeepSeekR1NodeTable",
        )
        cdk.CfnOutput(
            self,
            "ProxyLambdaArn",
            value=proxy_lambda.function_arn,
            export_name="DeepSeekR1ProxyLambdaArn",
        )


app = cdk.App()
DeepSeekApiStack(
    app,
    "DeepSeekApiStack",
    env=cdk.Environment(
        account=os.environ.get("CDK_DEFAULT_ACCOUNT"),
        region=os.environ.get("CDK_DEFAULT_REGION", "us-east-1"),
    ),
)
app.synth()
