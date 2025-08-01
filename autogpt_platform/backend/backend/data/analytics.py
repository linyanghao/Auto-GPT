import logging

import prisma.types

from backend.util.json import SafeJson

logger = logging.getLogger(__name__)


async def log_raw_analytics(
    user_id: str,
    type: str,
    data: dict,
    data_index: str,
):
    details = await prisma.models.AnalyticsDetails.prisma().create(
        data=prisma.types.AnalyticsDetailsCreateInput(
            userId=user_id,
            type=type,
            data=SafeJson(data),
            dataIndex=data_index,
        )
    )
    return details


async def log_raw_metric(
    user_id: str,
    metric_name: str,
    metric_value: float,
    data_string: str,
):
    if metric_value < 0:
        raise ValueError("metric_value must be non-negative")

    result = await prisma.models.AnalyticsMetrics.prisma().create(
        data=prisma.types.AnalyticsMetricsCreateInput(
            value=metric_value,
            analyticMetric=metric_name,
            userId=user_id,
            dataString=data_string,
        )
    )

    return result
