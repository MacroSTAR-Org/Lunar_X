from typing import Dict, List, Optional, TypedDict

import requests

from core.logger import logger

from .market_sources import (
    kGithubRawBaseUrl,
    kMarketBaseUrl,
)


class PluginManifest(TypedDict):
    id: str
    name: str
    version: str
    description: str
    author: str


class PluginMarketClient:
    """
    多源插件市场客户端
    优先使用 Unisphere，失败时从 GitHub PluginIndex 获取
    """

    def __init__(self, config: Dict):
        self.config = config
        self.plugins_index_repo = config.get(
            "plugins_index_repo", "Unisphere-Platform/LunarXU"
        )
        self.github_mirror = config.get("github_mirror", "").strip()
        self.github_pat = config.get("github_pat", "").strip()
        self.source_order = config.get("market_source_order", ["unisphere", "github"])
        self.github_raw_base_url = kGithubRawBaseUrl
        self._session = requests.Session()
        self._setup_headers()
        self._github_branch = self._detect_default_branch()

    def _detect_default_branch(self) -> str:
        """检测仓库的默认分支名"""
        try:
            url = f"https://api.github.com/repos/{self.plugins_index_repo}"
            response = self._session.get(url, timeout=10)
            if response.status_code == 200:
                data = response.json()
                branch = data.get("default_branch", "main")
                logger.info(f"检测到仓库默认分支: {branch}")
                return branch
        except requests.RequestException:
            pass
        return "main"

    def _setup_headers(self):
        """设置请求头"""
        self._session.headers.update(
            {
                "User-Agent": "Lunar-X-Plugin-Market/1.0",
            }
        )
        if self.github_pat:
            self._session.headers["Authorization"] = f"token {self.github_pat}"

    def _fetch_plugins_from_github_index(self) -> List[Dict]:
        """
        从 GitHub 仓库的 plugins.json 获取插件列表
        """
        plugins = []
        plugins_url = (
            f"{self.github_raw_base_url}/{self.plugins_index_repo}"
            f"/{self._github_branch}/plugins.json"
        )
        try:
            response = self._session.get(plugins_url, timeout=10)
            if response.status_code != 200:
                logger.warning(f"获取 plugins.json 失败: HTTP {response.status_code}")
                return []

            data = response.json()
            if not isinstance(data, list):
                logger.warning("plugins.json 格式不正确，期望列表")
                return []

            for item in data:
                if not isinstance(item, dict):
                    continue
                plugin_id = item.get("name", "")
                if not plugin_id:
                    continue
                plugins.append(
                    {
                        "id": plugin_id,
                        "name": item.get("display_name", plugin_id),
                        "version": item.get("version", "1.0.0"),
                        "description": item.get("description", ""),
                        "author": item.get("author", ""),
                        "path": plugin_id,
                    }
                )

            logger.info(f"从 GitHub plugins.json 解析到 {len(plugins)} 个插件")
            return plugins

        except requests.RequestException as e:
            logger.warning(f"获取 plugins.json 失败: {e}")
            return []

    def _fetch_unisphere_plugins(self) -> List[Dict]:
        """
        从 Unisphere 获取插件列表
        """
        plugins = []

        try:
            url = f"{kMarketBaseUrl}/api/v1/catalog"
            response = self._session.get(url, timeout=30)

            if response.status_code == 200:
                catalog_data = response.json()
                if isinstance(catalog_data, dict):
                    plugins_list = catalog_data.get("plugins", [])
                    if isinstance(plugins_list, list):
                        for item in plugins_list:
                            if isinstance(item, dict):
                                plugin = {
                                    "id": item.get("id", ""),
                                    "name": item.get("name", ""),
                                    "version": item.get("version", "0.1.0"),
                                    "description": item.get("description", ""),
                                    "download_url": item.get("downloadUrl", ""),
                                }
                                if plugin["id"] and plugin["name"]:
                                    plugins.append(plugin)
                        logger.info(f"从 Unisphere 获取到 {len(plugins)} 个插件")

        except requests.RequestException as e:
            logger.error(f"从 Unisphere 获取插件失败: {e}")

        return plugins

    def get_available_plugins(self, installed_plugin_names: set = None) -> List[Dict]:
        """
        获取可用插件列表

        实现多源优先级逻辑：
        1. 按 source_order 顺序依次获取各数据源的插件
        2. 高优先级源的插件覆盖低优先级源的同名插件
        """
        installed_plugin_names = installed_plugin_names or set()
        all_plugins: Dict[str, Dict] = {}

        for source in self.source_order:
            if source == "unisphere":
                plugins = self._fetch_unisphere_plugins()
                for p in plugins:
                    pid = p.get("id")
                    if pid:
                        p["url"] = p.pop("download_url", "")
                        p["path"] = pid
                        all_plugins[pid] = p
                logger.info(f"Unisphere: {len(plugins)} 插件")
            elif source == "github":
                plugins = self._fetch_plugins_from_github_index()
                for p in plugins:
                    pid = p.get("id")
                    if pid and pid not in all_plugins:
                        p["url"] = self._build_github_download_url()
                        all_plugins[pid] = p
                logger.info(f"GitHub: {len(plugins)} 插件")

        final_plugins = [
            p for pid, p in all_plugins.items() if pid not in installed_plugin_names
        ]

        logger.info(f"最终可用插件: {len(final_plugins)} 个")
        return final_plugins

    def _build_github_download_url(self) -> str:
        base_url = f"https://github.com/{self.plugins_index_repo}/archive/refs/heads/{self._github_branch}.zip"
        if self.github_mirror:
            return f"{self.github_mirror}{base_url}"
        return base_url

    def get_plugin_download_url(self, plugin_id: str, checksum: str = None) -> str:
        """
        获取插件的下载 URL

        优先级：
        1. Unisphere 提供的 download_url
        2. GitHub 提供的仓库 URL
        """
        download_url = ""

        for source in self.source_order:
            if source == "unisphere":
                try:
                    url = f"{kMarketBaseUrl}/api/v1/plugins/{plugin_id}"
                    response = self._session.get(url, timeout=30)
                    if response.status_code == 200:
                        plugin_data = response.json()
                        if isinstance(plugin_data, dict):
                            download_url = plugin_data.get("downloadUrl", "")
                            if download_url:
                                logger.info(f"从 Unisphere 获取 {plugin_id} 的下载地址")
                                return download_url
                except requests.RequestException:
                    pass

            elif source == "github":
                try:
                    download_url = self._build_github_download_url()
                    logger.info(f"从 GitHub 获取 {plugin_id} 的下载地址")
                    return download_url
                except Exception:
                    pass

        if not download_url:
            logger.error(f"无法获取 {plugin_id} 的下载地址")

        return download_url

    def resolve_checksum(self, plugin_id: str) -> Optional[str]:
        """
        获取插件的 SHA256 校验和
        目前 Unisphere 会提供，GitHub 上暂时没有
        """
        try:
            url = f"{kMarketBaseUrl}/api/v1/plugins/{plugin_id}"
            response = self._session.get(url, timeout=30)
            if response.status_code == 200:
                plugin_data = response.json()
                if isinstance(plugin_data, dict):
                    return plugin_data.get("sha256")
        except requests.RequestException:
            pass

        return None
