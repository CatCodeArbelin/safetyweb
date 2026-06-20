"""X-UI node selection service."""

from dataclasses import dataclass

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, XuiNodeConfig
from app.db.models import Payment, PaymentStatus, Subscription, SubscriptionStatus
from app.db.session import async_session_maker


class NoAvailableNodeError(RuntimeError):
    """Raised when no configured X-UI node can accept another subscription."""


@dataclass(frozen=True, slots=True)
class NodeCapacityInfo:
    """Current capacity snapshot for a configured X-UI node."""

    key: str
    name: str
    enabled: bool
    active_count: int
    pending_reservations: int
    occupied_count: int
    max_active_subscriptions: int | None
    free_slots: int | None
    has_capacity: bool


async def lock_capacity_selection(session: AsyncSession) -> None:
    """Serialize X-UI node capacity selection within the current transaction."""
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtext('xui_node_capacity_selection'))")
    )


async def get_node_occupancy_counts(
    session: AsyncSession,
    *,
    exclude_payment_id: int | None = None,
) -> dict[str, tuple[int, int]]:
    """Return active subscription and unexpired pending payment counts by node key."""
    active_result = await session.execute(
        select(Subscription.node_key, func.count(Subscription.id))
        .where(Subscription.status == SubscriptionStatus.ACTIVE)
        .group_by(Subscription.node_key)
    )
    active_counts = {node_key: int(count) for node_key, count in active_result.all()}

    reserved_statement = (
        select(Payment.reserved_node_key, func.count(Payment.id))
        .where(
            Payment.status == PaymentStatus.PENDING,
            Payment.reserved_node_key.is_not(None),
            Payment.node_reservation_expires_at > func.now(),
        )
        .group_by(Payment.reserved_node_key)
    )
    if exclude_payment_id is not None:
        reserved_statement = reserved_statement.where(Payment.id != exclude_payment_id)

    reserved_result = await session.execute(reserved_statement)
    reserved_counts = {
        node_key: int(count)
        for node_key, count in reserved_result.all()
        if node_key is not None
    }

    node_keys = set(active_counts) | set(reserved_counts)
    return {
        node_key: (active_counts.get(node_key, 0), reserved_counts.get(node_key, 0))
        for node_key in node_keys
    }


class NodeSelectorService:
    """Select configured X-UI nodes for subscription provisioning."""

    def __init__(
        self,
        settings: Settings | None = None,
        session: AsyncSession | None = None,
    ) -> None:
        self.settings = settings or Settings()
        self.session = session

    async def select_node_for_new_subscription(
        self,
        *,
        exclude_payment_id: int | None = None,
    ) -> XuiNodeConfig:
        """Return an enabled node with available subscription capacity."""
        if self.session is not None:
            return await self._select_node_for_new_subscription(
                self.session,
                exclude_payment_id=exclude_payment_id,
            )

        async with async_session_maker() as session:
            return await self._select_node_for_new_subscription(
                session,
                exclude_payment_id=exclude_payment_id,
            )

    async def select_preferred_node_for_new_subscription(
        self,
        preferred_node_key: str,
        *,
        exclude_payment_id: int | None = None,
    ) -> XuiNodeConfig | None:
        """Return the preferred node when it exists and has capacity.

        Unknown node keys are ignored so callers can fall back to automatic selection.
        """
        if self.session is not None:
            return await self._select_preferred_node_for_new_subscription(
                self.session,
                preferred_node_key,
                exclude_payment_id=exclude_payment_id,
            )

        async with async_session_maker() as session:
            return await self._select_preferred_node_for_new_subscription(
                session,
                preferred_node_key,
                exclude_payment_id=exclude_payment_id,
            )

    async def get_capacity_snapshot(self) -> list[NodeCapacityInfo]:
        """Return the current configured node capacity snapshot."""
        if self.session is not None:
            return await self._get_capacity_snapshot(self.session)

        async with async_session_maker() as session:
            return await self._get_capacity_snapshot(session)

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
        *,
        exclude_payment_id: int | None = None,
    ) -> XuiNodeConfig:
        await lock_capacity_selection(session)
        occupancy_counts = await get_node_occupancy_counts(
            session,
            exclude_payment_id=exclude_payment_id,
        )
        eligible_nodes: list[tuple[int, int, str, XuiNodeConfig]] = []

        for node in self.settings.xui_nodes:
            if not node.enabled:
                continue

            active_count, reserved_count = occupancy_counts.get(node.key, (0, 0))
            limit = node.max_active_subscriptions
            if limit is None:
                limit = self.settings.xui_default_max_active_subscriptions

            has_capacity = limit is None or active_count + reserved_count < limit
            if not has_capacity:
                continue

            eligible_nodes.append(
                (active_count + reserved_count, -node.weight, node.key, node)
            )

        if not eligible_nodes:
            msg = "No enabled XUI nodes with available subscription capacity are configured"
            raise NoAvailableNodeError(msg)

        return min(eligible_nodes, key=lambda item: item[:3])[3]

    async def _select_preferred_node_for_new_subscription(
        self,
        session: AsyncSession,
        preferred_node_key: str,
        *,
        exclude_payment_id: int | None = None,
    ) -> XuiNodeConfig | None:
        await lock_capacity_selection(session)
        try:
            node = self.settings.get_xui_node(preferred_node_key)
        except KeyError:
            return None
        if not node.enabled:
            msg = f"Preferred XUI node '{preferred_node_key}' is disabled"
            raise NoAvailableNodeError(msg)

        occupancy_counts = await get_node_occupancy_counts(
            session,
            exclude_payment_id=exclude_payment_id,
        )
        active_count, reserved_count = occupancy_counts.get(node.key, (0, 0))
        limit = node.max_active_subscriptions
        if limit is None:
            limit = self.settings.xui_default_max_active_subscriptions
        if limit is not None and active_count + reserved_count >= limit:
            msg = f"Preferred XUI node '{preferred_node_key}' has no capacity"
            raise NoAvailableNodeError(msg)
        return node

    async def _get_capacity_snapshot(
        self,
        session: AsyncSession,
    ) -> list[NodeCapacityInfo]:
        """Build capacity rows for all configured X-UI nodes."""
        occupancy_counts = await get_node_occupancy_counts(session)
        snapshot: list[NodeCapacityInfo] = []
        for node in self.settings.xui_nodes:
            active_count, reserved_count = occupancy_counts.get(node.key, (0, 0))
            limit = node.max_active_subscriptions
            if limit is None:
                limit = self.settings.xui_default_max_active_subscriptions
            occupied_count = active_count + reserved_count
            free_slots = None if limit is None else max(limit - occupied_count, 0)
            has_capacity = node.enabled and (limit is None or occupied_count < limit)
            snapshot.append(
                NodeCapacityInfo(
                    key=node.key,
                    name=node.name or node.key,
                    enabled=node.enabled,
                    active_count=active_count,
                    pending_reservations=reserved_count,
                    occupied_count=occupied_count,
                    max_active_subscriptions=limit,
                    free_slots=free_slots,
                    has_capacity=has_capacity,
                )
            )
        return snapshot

    @staticmethod
    async def _get_active_subscription_counts(
        session: AsyncSession,
    ) -> dict[str, int]:
        """Return active subscription counts by node key."""
        occupancy_counts = await get_node_occupancy_counts(session)
        return {
            node_key: active_count
            for node_key, (active_count, _reserved_count) in occupancy_counts.items()
        }
