#!/usr/bin/env python3
"""Entrypoint to the cognito alb fargate demo CDK app"""

# from aws_cdk import core
import aws_cdk as core

from infrastructure.demo_stack import DemoStack
from infrastructure.configuration import get_config

def main():
    env = core.Environment(account="465521767555", region="ap-southeast-2")

    """Wrapper for the CDK app"""
    app = core.App()

    DemoStack(
        app,
        "cognito-alb-fargate-demo",
        config=get_config(),
        env=env
    )

    app.synth()


if __name__ == "__main__":
    main()
