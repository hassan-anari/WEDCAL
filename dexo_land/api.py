from ninja import NinjaAPI

from apps.base_tap.api import router as base_tap_router
from apps.clans.api import router as clans_router
from apps.daily.api import router as daily_router
from apps.missions.api import router as missions_router
from apps.moderation.api import router as moderation_router
from apps.network.api import router as network_router
from apps.profile_collection.api import router as profile_router
from apps.referrals.api import router as referrals_router
from apps.rewards.api import bootstrap_router, rewards_router
from apps.telegram.api import auth_router, webhook_router
from apps.typing_game.api import router as typing_router

api = NinjaAPI(title="Dexo Land API", version="1.0.0", urls_namespace="api-v1")
api.add_router("/auth", auth_router)
api.add_router("/telegram", webhook_router)
api.add_router("/bootstrap", bootstrap_router)
api.add_router("/rewards", rewards_router)
api.add_router("/daily", daily_router)
api.add_router("/missions", missions_router)
api.add_router("/typing", typing_router)
api.add_router("/referrals", referrals_router)
api.add_router("/network", network_router)
api.add_router("/base", base_tap_router)
api.add_router("/profile", profile_router)
api.add_router("/clans", clans_router)
api.add_router("/moderation", moderation_router)
