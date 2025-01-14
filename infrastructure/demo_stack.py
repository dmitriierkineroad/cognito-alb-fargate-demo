"""Contains the stack with the resources for this app."""

import os
import urllib.parse

import aws_cdk.aws_certificatemanager as certificatemanager
import aws_cdk.aws_cognito as cognito
import aws_cdk.aws_ec2 as ec2
import aws_cdk.aws_ecs as ecs
import aws_cdk.aws_ecs_patterns as ecs_patterns
import aws_cdk.aws_ecr_assets as ecr_assets
import aws_cdk.aws_elasticloadbalancingv2 as elb
import aws_cdk.aws_elasticloadbalancingv2_actions as elb_actions
import aws_cdk.aws_lambda as _lambda
import aws_cdk.aws_route53 as route53
import aws_cdk.aws_ssm as ssm

# from aws_cdk import core
import aws_cdk as core
from constructs import Construct

import infrastructure.configuration as configuration

class DemoStack(core.Stack):
    """
    Provisions a Cognito User Pool with a custom domain as well as
    a VPC with an ALB in front of an ECS service based on Fargate.
    """

    config: configuration.Config

    user_pool: cognito.UserPool
    user_pool_custom_domain: cognito.UserPoolDomain
    user_pool_client: cognito.UserPoolClient

    user_pool_full_domain: str
    user_pool_logout_url: str
    user_pool_user_info_url: str

    # def __init__(self, scope: core.Construct, id: str,
    #              config: configuration.Config,  **kwargs) -> None:
    def __init__(self, scope: Construct, id: str,
                 config: configuration.Config, **kwargs) -> None:
        super().__init__(scope, id, **kwargs)

        # We import the fleet scheduling VPC ID
        fleetSchedulingServiceEcsVpcId = ssm.StringParameter.value_from_lookup(
            self,
            parameter_name="FleetSchedulingServiceEcsVpcId"
        )
        # We import the fleet scheduling VPC
        vpc = ec2.Vpc.from_lookup(
            self,
            id="FleetSchedulingServiceEcsVpcImported",
            vpc_id=fleetSchedulingServiceEcsVpcId
        )

        self.config = config

        self.add_cognito(vpc)

        self.add_webapp(vpc)

    def add_cognito(self, vpc):
        """
        Sets up the cognito infrastructure with the user pool, custom domain
        and app client for use by the ALB.
        """
        # Create the user pool that holds our users
        self.user_pool = cognito.UserPool(
            self,
            "user-pool",
            account_recovery=cognito.AccountRecovery.EMAIL_AND_PHONE_WITHOUT_MFA,
            auto_verify=cognito.AutoVerifiedAttrs(email=True, phone=True),
            self_sign_up_enabled=True,
            standard_attributes=cognito.StandardAttributes(
                email=cognito.StandardAttribute(mutable=True, required=True),
                given_name=cognito.StandardAttribute(mutable=True, required=True),
                family_name=cognito.StandardAttribute(mutable=True, required=True)
            )
        )

        # Add a lambda function that automatically confirms new users without
        # email/phone verification, just for this demo
        auto_confirm_function = _lambda.Function(
            self,
            "auto-confirm-function",
            code=_lambda.Code.from_asset(
                path=os.path.join(os.path.dirname(__file__), "..", "auto_confirm_function")
            ),
            handler="lambda_handler.lambda_handler",
            # runtime=_lambda.Runtime.PYTHON_3_8,
            runtime=_lambda.Runtime.PYTHON_3_9,
            vpc=vpc
        )

        self.user_pool.add_trigger(
            operation=cognito.UserPoolOperation.PRE_SIGN_UP,
            fn=auto_confirm_function
        )

        # Add a custom domain for the hosted UI
        self.user_pool_custom_domain = self.user_pool.add_domain(
            "user-pool-domain",
            cognito_domain=cognito.CognitoDomainOptions(
                domain_prefix=self.config.cognito_custom_domain
            )
        )

        # Create an app client that the ALB can use for authentication
        self.user_pool_client = self.user_pool.add_client(
            "alb-app-client",
            user_pool_client_name="AlbAuthentication",
            generate_secret=True,
            o_auth=cognito.OAuthSettings(
                callback_urls=[
                    # This is the endpoint where the ALB accepts the
                    # response from Cognito
                    f"https://{self.config.application_dns_name}/oauth2/idpresponse",

                    # This is here to allow a redirect to the login page
                    # after the logout has been completed
                    f"https://{self.config.application_dns_name}"
                ],
                flows=cognito.OAuthFlows(authorization_code_grant=True),
                scopes=[
                    cognito.OAuthScope.OPENID
                ]
            ),
            supported_identity_providers=[
                cognito.UserPoolClientIdentityProvider.COGNITO
            ]
        )

        # Logout URLs and redirect URIs can't be set in CDK constructs natively ...yet
        user_pool_client_cf: cognito.CfnUserPoolClient = self.user_pool_client.node.default_child
        user_pool_client_cf.logout_ur_ls = [
            # This is here to allow a redirect to the login page
            # after the logout has been completed
            f"https://{self.config.application_dns_name}"
        ]

        self.user_pool_full_domain = self.user_pool_custom_domain.base_url()
        redirect_uri = urllib.parse.quote('https://' + self.config.application_dns_name)
        self.user_pool_logout_url = f"{self.user_pool_full_domain}/logout?" \
                                    + f"client_id={self.user_pool_client.user_pool_client_id}&" \
                                    + f"logout_uri={redirect_uri}"

        self.user_pool_user_info_url = f"{self.user_pool_full_domain}/oauth2/userInfo"

    def add_webapp(self, vpc):
        """
        Adds the ALB, ECS-Service and Cognito Login Action on the ALB.
        """

        # # Create the ecs cluster to house our service, this also creates a VPC in 2 AZs
        # cluster = ecs.Cluster(
        #     self,
        #     "cluster"
        # )

        # Load the hosted zone
        hosted_zone = route53.HostedZone.from_hosted_zone_attributes(
            self,
            "hosted-zone",
            hosted_zone_id=self.config.hosted_zone_id,
            zone_name=self.config.hosted_zone_name
        )

        # Create a Certificate for the ALB
        certificate = certificatemanager.DnsValidatedCertificate(
            self,
            "certificate",
            hosted_zone=hosted_zone,
            domain_name=self.config.application_dns_name
        )

        # Define the Docker Image for our container (the CDK will do the build and push for us!)
        docker_image = ecr_assets.DockerImageAsset(
            self,
            "jwt-app",
            directory=os.path.join(os.path.dirname(__file__), "..", "src")
        )

        # This creates the ALB with an ECS Service on Fargate
        fargate_service = ecs_patterns.ApplicationLoadBalancedFargateService(
            self,
            "fargate-service",
            # cluster=cluster,
            vpc=vpc,
            certificate=certificate,
            domain_name=self.config.application_dns_name,
            domain_zone=hosted_zone,
            desired_count=int(self.config.backend_desired_count),
            task_image_options=ecs_patterns.ApplicationLoadBalancedTaskImageOptions(
                image=ecs.ContainerImage.from_docker_image_asset(docker_image),
                environment={
                    "PORT": "80",
                    "LOGOUT_URL": self.user_pool_logout_url,
                    "USER_INFO_URL": self.user_pool_user_info_url,
                }
            ),
            redirect_http=True,
            health_check_grace_period=core.Duration.days(365)
        )

        # Configure the health checks to use our /healthcheck endpoint
        fargate_service.target_group.configure_health_check(
            enabled=True,
            path="/healthcheck",
            healthy_http_codes="200"
        )

        # Add an additional HTTPS egress rule to the Load Balancers
        # security group to talk to Cognito, by default the construct
        # doesn't allow the ALB to make an outbound request
        lb_security_group = fargate_service.load_balancer.connections.security_groups[0]

        lb_security_group.add_egress_rule(
            peer=ec2.Peer.any_ipv4(),
            connection=ec2.Port(
                protocol=ec2.Protocol.TCP,
                string_representation="443",
                from_port=443,
                to_port=443
            ),
            description="Outbound HTTPS traffic to get to Cognito"
        )

        # Allow 10 seconds for in flight requests before termination,
        # the default of 5 minutes is much too high.
        fargate_service.target_group.set_attribute(
            key="deregistration_delay.timeout_seconds",
            value="10"
        )

        # # Add the authentication actions as a rule with priority
        # fargate_service.listener.add_action(
        #     "authenticate-rule",
        #     priority=1000,
        #     action=elb_actions.AuthenticateCognitoAction(
        #         next=elb.ListenerAction.forward(
        #             target_groups=[
        #                 fargate_service.target_group
        #             ]
        #         ),
        #         user_pool=self.user_pool,
        #         user_pool_client=self.user_pool_client,
        #         user_pool_domain=self.user_pool_custom_domain,
        #
        #     ),
        #     host_header=self.config.application_dns_name
        # )

        # Enable authentication on the Load Balancer
        alb_listener: elb.CfnListener = fargate_service.listener.node.default_child

        elb.CfnListenerRule(
            self,
            "authenticate-rule",
            actions=[
                {
                    "type": "authenticate-cognito",
                    "authenticateCognitoConfig": elb.CfnListenerRule.AuthenticateCognitoConfigProperty(
                        user_pool_arn=self.user_pool.user_pool_arn,
                        user_pool_client_id=self.user_pool_client.user_pool_client_id,
                        user_pool_domain=self.user_pool_custom_domain.domain_name
                    ),
                    "order": 1
                },
                {
                    "type": "forward",
                    "order": 10,
                    "targetGroupArn": fargate_service.target_group.target_group_arn
                }
            ],
            conditions=[
                {
                    "field": "host-header",
                    "hostHeaderConfig": {
                        "values": [
                            f"{self.config.application_dns_name}"
                        ]
                    }
                }
            ],
            # Reference the Listener ARN
            listener_arn=alb_listener.ref,
            priority=1000
        )

        # Overwrite the default action to show a 403 fixed response in case somebody
        # accesses the website via the alb URL directly
        cfn_listener: elb.CfnListener = fargate_service.listener.node.default_child
        cfn_listener.default_actions = [{
            "type": "fixed-response",
            "fixedResponseConfig": {
                "statusCode": "403",
                "contentType": "text/plain",
                "messageBody": "This is not a valid endpoint!"
            }
        }]
