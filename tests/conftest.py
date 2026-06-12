"""Make the Lambda package's pure ``selection`` module importable in tests.

The Lambda handlers live in ``infra/aws-cdk/lambda`` and are deployed flat, so
``selection.py`` is imported as a top-level module at runtime. We mirror that
here by putting the lambda directory on ``sys.path`` rather than importing the
boto3-coupled handlers.
"""
import os
import sys

LAMBDA_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "infra",
    "aws-cdk",
    "lambda",
)

if LAMBDA_DIR not in sys.path:
    sys.path.insert(0, LAMBDA_DIR)
