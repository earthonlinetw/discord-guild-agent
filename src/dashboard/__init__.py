"""Dashboard 服務與 Web 面板模組。"""

from src.dashboard.service import DashboardService, DashboardDataProvider
from src.dashboard.web import DashboardServer

__all__ = ["DashboardService", "DashboardDataProvider", "DashboardServer"]
