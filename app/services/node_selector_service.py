"""X-UI node selection service."""

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, XuiNodeConfig
from app.db.models import Subscription, SubscriptionStatus
from app.db.session import async_session_maker


class NodeSelectorService:
    """Select configured X-UI nodes for subscription provisioning."""

    def __init__(
        self,
        settings: Settings | None = None,
        session: AsyncSession | None = None,
    ) -> None:
        self.settings = settings or Settings()
        self.session = session

    async def select_node_for_new_subscription(self) -> XuiNodeConfig:
        """Return an enabled node with the smallest active subscription count."""
        if self.session is not None:
            return await self._select_node_for_new_subscription(self.session)

        async with async_session_maker() as session:
            return await self._select_node_for_new_subscription(session)

    def get_node_for_subscription(self, subscription: Subscription) -> XuiNodeConfig:
        """Return the configured node assigned to a subscription."""
        node_key = subscription.node_key
        try:
            return self.settings.get_xui_node(node_key)
        except KeyError as exc:
            msg = f"XUI node '{node_key}' is not configured"
            raise ValueError(msg) from exc

    async def _select_node_for_new_subscription(
        self,
        session: AsyncSession,
    ) -> XuiNodeConfig:
        active_counts = await self._get_active_subscription_counts(session)
        eligible_nodes: list[tuple[int, int, XuiNodeConfig]] = []
        for index, node in enumerate(self.settings.xui_nodes):
            if not node.enabled:
                continue
            active_count = active_counts.get(node.key, 0)
            if (
                node.max_active_subscriptions is not None
                and active_count >= node.max_active_subscriptions
            ):
                continue
            eligible_nodes.append((active_count, index, node))

        if not eligible_nodes:
            msg = "No enabled XUI nodes with available subscription capacity are configured"
            raise RuntimeError(msg)

        return min(eligible_nodes, key=lambda item: (item[0], item[1]))[2]

    @staticmethod
    async def _get_active_subscription_counts(
        session: AsyncSession,
    ) -> dict[str, int]:
        result = await session.execute(
            select(Subscription.node_key, func.count(Subscription.id))
            .where(Subscription.status == SubscriptionStatus.ACTIVE)
            .group_by(Subscription.node_key)
        )
        return {node_key: int(count) for node_key, count in result.all()}
