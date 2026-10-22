import logging

# The Azure SDK logs every HTTP request and response (URL, headers) at INFO.
# Kept to warnings, so Application Insights receives the collector's own logs
# and not thousands of SDK lines an hour.
for _name in ("azure", "azure.core.pipeline.policies.http_logging_policy", "azure.identity", "msal"):
    logging.getLogger(_name).setLevel(logging.WARNING)
