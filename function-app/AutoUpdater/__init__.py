"""AutoUpdater — Timer trigger that checks for and applies updates daily at 3 AM UTC."""

import logging
import os

import azure.functions as func

logger = logging.getLogger(__name__)


def main(timer: func.TimerRequest) -> None:
    from shared.updater import check_and_apply_update, _post_deploy_health_check

    # Audit logging is best-effort — import inline so AutoUpdater still works
    # if debug_logger has issues.
    try:
        from shared.debug_logger import log_event
    except Exception:  # pragma: no cover — defensive
        def log_event(*_a, **_kw):  # type: ignore
            return None

    if timer.past_due:
        logger.info("AutoUpdater timer is past due — running now")

    logger.info("AutoUpdater: checking for updates ...")
    result = check_and_apply_update(auto_apply=True)
    logger.info(f"AutoUpdater result: {result}")

    action = result.get("action", "unknown")

    # Audit-log every AutoUpdater run so ops can see history even if the
    # new build breaks and Function-level logging looks weird afterwards.
    try:
        log_event(
            "auto_update_run",
            action=action,
            local_version=result.get("local_version"),
            remote_version=result.get("remote_version"),
            message=result.get("message", ""),
        )
    except Exception as e:
        logger.warning("Could not audit-log AutoUpdater run: %s", e)

    if action == "deployed":
        logger.info(
            f"Successfully updated from {result['local_version']} "
            f"to {result['remote_version']}"
        )
        # Post-deploy health check — informational only, no rollback.
        # Runs in the OLD process (we're the instance that just kicked off
        # the deploy). Gives ops a durable record of whether the new build
        # actually starts.
        func_app_name = os.environ.get("WEBSITE_SITE_NAME", "")
        if func_app_name:
            try:
                health = _post_deploy_health_check(func_app_name)
                try:
                    log_event(
                        "auto_update_health_check",
                        healthy=health.get("healthy"),
                        checks=health.get("checks"),
                        deployed_version=result.get("remote_version"),
                    )
                except Exception:
                    pass
                if not health.get("healthy"):
                    logger.error(
                        "Post-deploy health check FAILED for version %s: %s",
                        result.get("remote_version"), health,
                    )
                else:
                    logger.info("Post-deploy health check OK: %s", health)
            except Exception as e:
                logger.warning("Post-deploy health check crashed: %s", e)
    elif action == "deploy_failed":
        logger.error(
            f"Update deployment failed: {result.get('deploy_result', {}).get('error', 'unknown')}"
        )
    elif action == "up_to_date":
        logger.info(f"Already on latest version ({result['local_version']})")
    else:
        logger.info(f"Update check: {result.get('message', action)}")
