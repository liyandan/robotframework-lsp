from functools import partial
import os
from pathlib import Path
from typing import List, Any, Optional, Dict, Union

from robocorp_code import commands
from concurrent.futures import Future
from robocorp_code.protocols import (
    IRccWorkspace,
    IRccRobotMetadata,
    LocalRobotMetadataInfoDict,
    WorkspaceInfoDict,
    PackageInfoDict,
    ActionResultDict,
    UploadRobotParamsDict,
    UploadNewRobotParamsDict,
    CreateRobotParamsDict,
    CloudListWorkspaceDict,
    CloudLoginParamsDict,
    ListWorkspacesActionResultDict,
    PackageInfoInLRUDict,
    RunInRccParamsDict,
    ActionResultDictLocalRobotMetadata,
    ActionResultDictRobotLaunch,
    TypedDict,
)
from robocorp_ls_core.basic import overrides
from robocorp_ls_core.cache import CachedFileInfo
from robocorp_ls_core.protocols import IConfig
from robocorp_ls_core.python_ls import PythonLanguageServer
from robocorp_ls_core.robotframework_log import get_logger
from robocorp_code.locators.locator_protocols import BrowserLocatorTypedDict


log = get_logger(__name__)

try:
    __file__ = os.path.abspath(__file__)
except NameError:
    pass  # During pydevd debugger auto reload __file__ may not be there.
else:
    if __file__.endswith((".pyc", ".pyo")):
        __file__ = __file__[:-1]


class _RegisteredCommand(object):
    def __init__(self, command_name, expected_return_cls):
        self.command_name = command_name
        self.expected_return_type = expected_return_cls
        self.func = None


class _CommandDispatcher(object):
    def __init__(self):
        self._command_name_to_registered_command = {}

    def __call__(self, command_name, expected_return_cls=None):
        """
        :param expected_return_type:
            If None, the default expected is the ActionResultDict!
        """
        if isinstance(command_name, str):
            self._curr_registered_command = _RegisteredCommand(
                command_name, expected_return_cls
            )
            return self
        else:
            self._curr_registered_command.func = command_name
            self._command_name_to_registered_command[
                self._curr_registered_command.command_name
            ] = self._curr_registered_command
            return self._curr_registered_command.func

    def dispatch(self, language_server, command_name, arguments) -> ActionResultDict:
        try:
            registered_command = self._command_name_to_registered_command[command_name]
            func = registered_command.func

            if registered_command.expected_return_type is None:
                ret: ActionResultDict = func(language_server, *arguments)
                assert isinstance(ret, dict)
                assert "success" in ret
                return ret
            else:
                ret = func(language_server, *arguments)
                assert isinstance(ret, registered_command.expected_return_type)
                return ret

        except Exception as e:
            error_msg = f"Error in command: {command_name} with args: {arguments}.\n{e}"
            log.exception(error_msg)
            return {"success": False, "message": error_msg, "result": None}


command_dispatcher = _CommandDispatcher()


class ListWorkspaceCachedInfoDict(TypedDict):
    ws_info: List[WorkspaceInfoDict]
    account_cache_key: tuple


class BrowserLocatorContainer(TypedDict):
    name: str
    locator: BrowserLocatorTypedDict


class ActionResultDictBrowserLocatorContainer(TypedDict):
    success: bool
    message: Optional[
        str
    ]  # if success == False, this can be some message to show to the user
    result: Optional[BrowserLocatorContainer]


class _LocatorsInThreadAPI:
    """
    An API which allows managing the LocatorServerManager and LocatorsDB in
    a separate thread (note that most of its method calls return a Future).
    """

    def __init__(self):
        from concurrent.futures.thread import ThreadPoolExecutor

        # We only use 1 thread here!
        self._thread_pool = ThreadPoolExecutor(1)
        self._initialized = False

    def shutdown(self):
        self._thread_pool.shutdown(wait=False)

    def _initialize_in_thread(self):
        """
        This should be called in the thread when making the first call to the
        LocatorServerManager.
        """
        if self._initialized:
            return

        from robocorp_code.locators.server.locator_server_manager import (
            LocatorServerManager,
        )
        from robocorp_code.locators_db import LocatorsDB

        self._initialized = True
        self._locator_server_manager = LocatorServerManager()
        self._locators_db = LocatorsDB()

    def _initialize_and_call(self, func):
        self._initialize_in_thread()
        return func()

    def _submit_and_return_from_thread(self, func) -> Future:
        future = self._thread_pool.submit(self._initialize_and_call, func)
        return future

    def create_locator_from_browser_pick(self) -> "Future[ActionResultDict]":
        """
        Creates a locator from a browser pick.
        """
        return self._submit_and_return_from_thread(
            self._in_thread_create_locator_from_browser_pick
        )

    def _in_thread_create_locator_from_browser_pick(
        self
    ) -> ActionResultDictBrowserLocatorContainer:
        from typing import cast

        error_msg = self._locators_db.validate()
        if error_msg:
            return {"success": False, "message": error_msg, "result": None}

        result = self._locator_server_manager.browser_locator_pick()
        if result["success"]:
            # Ok, it did get the info for the browser locator pick, now, let's
            # add it to the json.
            try:
                browser_locator: BrowserLocatorTypedDict = cast(
                    BrowserLocatorTypedDict, result["result"]
                )
                name = self._locators_db.add_browser_locator(browser_locator)
                return {
                    "success": True,
                    "message": None,
                    "result": {"name": name, "locator": result["result"]},
                }

            except Exception as e:
                log.exception("Error creating browser locator.")
                return {"success": False, "message": str(e), "result": None}

        return result

    def browser_locator_stop(self) -> "Future[ActionResultDict]":
        """
        Stops the browser locator.
        """
        return self._submit_and_return_from_thread(self._in_thread_browser_locator_stop)

    def _in_thread_browser_locator_stop(self) -> ActionResultDict:
        return self._locator_server_manager.browser_locator_stop()

    def start_browser_locator(self, robot_yaml) -> "Future[ActionResultDict]":
        """
        Starts the browser locator and sets the robot yaml to be changed.
        """
        return self._submit_and_return_from_thread(
            partial(self._in_thread_start_browser_locator, robot_yaml=robot_yaml)
        )

    def _in_thread_start_browser_locator(self, robot_yaml) -> ActionResultDict:
        self._locators_db.set_robot_yaml_location(robot_yaml)
        return self._locator_server_manager.browser_locator_start()

    def set_robot_yaml_location(self, robot_yaml) -> "Future[None]":
        """
        Sets the robot yaml to be changed.
        """
        return self._submit_and_return_from_thread(
            partial(self._in_thread_set_robot_yaml_location, robot_yaml=robot_yaml)
        )

    def _in_thread_set_robot_yaml_location(self, robot_yaml) -> None:
        self._locators_db.set_robot_yaml_location(robot_yaml)


class RobocorpLanguageServer(PythonLanguageServer):

    # V2: save the account info along to validate user.
    CLOUD_LIST_WORKSPACE_CACHE_KEY = "CLOUD_LIST_WORKSPACE_CACHE_V2"
    PACKAGE_ACCESS_LRU_CACHE_KEY = "PACKAGE_ACCESS_LRU_CACHE"

    def __init__(self, read_stream, write_stream):
        from robocorp_code.rcc import Rcc
        from robocorp_ls_core.cache import DirCache
        from robocorp_ls_core.pluginmanager import PluginManager
        from robocorp_ls_core.ep_providers import DefaultConfigurationProvider
        from robocorp_ls_core.ep_providers import EPConfigurationProvider
        from robocorp_ls_core.ep_providers import DefaultDirCacheProvider
        from robocorp_ls_core.ep_providers import EPDirCacheProvider
        from robocorp_ls_core.ep_providers import DefaultEndPointProvider
        from robocorp_ls_core.ep_providers import EPEndPointProvider

        user_home = os.getenv("ROBOCORP_CODE_USER_HOME", None)
        if user_home is None:
            user_home = os.path.expanduser("~")
        cache_dir = os.path.join(user_home, ".robocorp-code", ".cache")

        log.debug(f"Cache dir: {cache_dir}")

        self._dir_cache = DirCache(cache_dir)
        self._rcc = Rcc(self)
        self._track = True
        self._local_list_robots_cache: Dict[
            Path, CachedFileInfo[LocalRobotMetadataInfoDict]
        ] = {}
        PythonLanguageServer.__init__(self, read_stream, write_stream)

        self._pm = PluginManager()
        self._config_provider = DefaultConfigurationProvider(self.config)
        self._pm.set_instance(EPConfigurationProvider, self._config_provider)
        self._pm.set_instance(
            EPDirCacheProvider, DefaultDirCacheProvider(self._dir_cache)
        )
        self._pm.set_instance(
            EPEndPointProvider, DefaultEndPointProvider(self._endpoint)
        )
        from robocorp_code.plugins.resolve_interpreter import register_plugins

        register_plugins(self._pm)
        self._locators_in_thread_api = _LocatorsInThreadAPI()

    @overrides(PythonLanguageServer.m_initialize)
    def m_initialize(
        self,
        processId=None,
        rootUri=None,
        rootPath=None,
        initializationOptions=None,
        workspaceFolders=None,
        **_kwargs,
    ) -> dict:
        ret = PythonLanguageServer.m_initialize(
            self,
            processId=processId,
            rootUri=rootUri,
            rootPath=rootPath,
            initializationOptions=initializationOptions,
            workspaceFolders=workspaceFolders,
        )

        if initializationOptions and isinstance(initializationOptions, dict):
            self._track = not initializationOptions.get("do-not-track", False)

        from robocorp_code import __version__

        self._feedback_metric("vscode.started", __version__)
        return ret

    def _feedback_metric(self, name, value="+1"):
        if not self._track:
            return

        from robocorp_ls_core.timeouts import TimeoutTracker

        timeout_tracker = TimeoutTracker.get_singleton()
        timeout_tracker.call_on_timeout(
            0.1, partial(self._rcc.feedack_metric, name, value)
        )

    def m_shutdown(self, **_kwargs):
        self._locators_in_thread_api.shutdown()
        PythonLanguageServer.m_shutdown(self, **_kwargs)

    @overrides(PythonLanguageServer.cancel_lint)
    def cancel_lint(self, doc_uri):
        pass  # no-op

    @overrides(PythonLanguageServer.lint)
    def lint(self, doc_uri, is_saved):
        pass  # no-op

    @overrides(PythonLanguageServer._create_config)
    def _create_config(self) -> IConfig:
        from robocorp_code.robocorp_config import RobocorpConfig

        return RobocorpConfig()

    @overrides(PythonLanguageServer.capabilities)
    def capabilities(self):
        from robocorp_ls_core.lsp import TextDocumentSyncKind
        from robocorp_code.commands import ALL_SERVER_COMMANDS

        server_capabilities = {
            "codeActionProvider": False,
            # "codeLensProvider": {
            #     "resolveProvider": False,  # We may need to make this configurable
            # },
            "completionProvider": {
                "resolveProvider": False  # We know everything ahead of time
            },
            "documentFormattingProvider": False,
            "documentHighlightProvider": False,
            "documentRangeFormattingProvider": False,
            "documentSymbolProvider": False,
            "definitionProvider": False,
            "executeCommandProvider": {"commands": ALL_SERVER_COMMANDS},
            "hoverProvider": True,
            "referencesProvider": False,
            "renameProvider": False,
            "foldingRangeProvider": False,
            "textDocumentSync": {
                "change": TextDocumentSyncKind.INCREMENTAL,
                "save": {"includeText": False},
                "openClose": True,
            },
            "workspace": {
                "workspaceFolders": {"supported": True, "changeNotifications": True}
            },
        }
        log.info("Server capabilities: %s", server_capabilities)
        return server_capabilities

    def m_workspace__execute_command(self, command=None, arguments=()) -> Any:
        return command_dispatcher.dispatch(self, command, arguments)

    @command_dispatcher(commands.ROBOCORP_IS_LOGIN_NEEDED_INTERNAL)
    def _is_login_needed_internal(self) -> ActionResultDict:
        from robocorp_ls_core.progress_report import progress_context

        with progress_context(
            self._endpoint, "Validating cloud credentials", self._dir_cache
        ):
            login_needed = not self._rcc.credentials_valid()
        return {"success": login_needed, "message": None, "result": login_needed}

    @command_dispatcher(commands.ROBOCORP_CLOUD_LOGIN_INTERNAL)
    def _cloud_login(self, params: CloudLoginParamsDict) -> ActionResultDict:
        from robocorp_ls_core.progress_report import progress_context

        self._feedback_metric("vscode.cloud.login")

        # When new credentials are added we need to remove existing caches.
        self._dir_cache.discard(self.CLOUD_LIST_WORKSPACE_CACHE_KEY)

        credentials = params["credentials"]
        with progress_context(
            self._endpoint, "Adding cloud credentials", self._dir_cache
        ):
            result = self._rcc.add_credentials(credentials)
            if not result.success:
                return result.as_dict()

            result = self._rcc.credentials_valid()
        return {"success": result, "message": None, "result": result}

    @command_dispatcher(commands.ROBOCORP_CLOUD_LOGOUT_INTERNAL)
    def _cloud_logout(self) -> ActionResultDict:
        from robocorp_ls_core.progress_report import progress_context

        self._feedback_metric("vscode.cloud.logout")

        # When credentials are removed we need to remove existing caches.
        self._dir_cache.discard(self.CLOUD_LIST_WORKSPACE_CACHE_KEY)

        with progress_context(
            self._endpoint, "Removing cloud credentials", self._dir_cache
        ):
            return self._rcc.remove_current_credentials().as_dict()

    @command_dispatcher(commands.ROBOCORP_SAVE_IN_DISK_LRU)
    def _save_in_disk_lru(self, params: dict) -> ActionResultDict:
        name = params["name"]
        entry = params["entry"]
        lru_size = params["lru_size"]
        try:
            cache_lru_list = self._dir_cache.load(name, list)
        except:
            cache_lru_list = []

        try:
            if cache_lru_list[0] == entry:
                # Nothing to do as it already matches.
                return {"success": True, "message": "", "result": entry}

            cache_lru_list.remove(entry)
        except:
            pass  # If empty or if entry is not there, just proceed.

        if len(cache_lru_list) >= lru_size:
            cache_lru_list = cache_lru_list[:-1]

        cache_lru_list.insert(0, entry)
        self._dir_cache.store(name, cache_lru_list)
        return {"success": True, "message": "", "result": entry}

    @command_dispatcher(commands.ROBOCORP_LOAD_FROM_DISK_LRU, list)
    def _load_from_disk_lru(self, params: dict) -> ActionResultDict:
        try:
            name = params["name"]
            cache_lru_list = self._dir_cache.load(name, list)
        except:
            cache_lru_list = []

        return cache_lru_list

    def _get_sort_key_info(self):
        try:
            cache_lru_list: List[PackageInfoInLRUDict] = self._dir_cache.load(
                self.PACKAGE_ACCESS_LRU_CACHE_KEY, list
            )
        except KeyError:
            cache_lru_list = []
        DEFAULT_SORT_KEY = 10
        ws_id_and_pack_id_to_lru_index: Dict = {}
        for i, entry in enumerate(cache_lru_list):

            if i >= DEFAULT_SORT_KEY:
                break

            if isinstance(entry, dict):
                ws_id = entry.get("workspace_id")
                pack_id = entry.get("package_id")
                if ws_id is not None and pack_id is not None:
                    key = (ws_id, pack_id)
                    ws_id_and_pack_id_to_lru_index[key] = i
        return ws_id_and_pack_id_to_lru_index

    @command_dispatcher(commands.ROBOCORP_CLOUD_LIST_WORKSPACES_INTERNAL)
    def _cloud_list_workspaces(
        self, params: CloudListWorkspaceDict
    ) -> ListWorkspacesActionResultDict:
        from robocorp_ls_core.progress_report import progress_context

        DEFAULT_SORT_KEY = 10
        package_info: PackageInfoDict
        ws_dict: WorkspaceInfoDict

        ws_id_and_pack_id_to_lru_index = self._get_sort_key_info()
        curr_account_info = self._rcc.last_verified_account_info
        if curr_account_info is None:
            curr_account_info = self._rcc.get_valid_account_info()
            if curr_account_info is None:
                return {
                    "success": False,
                    "message": "Unable to get workspace info (no user is logged in).",
                    "result": None,
                }

        account_cache_key = (curr_account_info.account, curr_account_info.identifier)

        if not params.get("refresh", True):
            try:
                cached: ListWorkspaceCachedInfoDict = self._dir_cache.load(
                    self.CLOUD_LIST_WORKSPACE_CACHE_KEY, dict
                )
            except KeyError:
                pass
            else:
                # We need to update the sort key when it's gotten from the cache.
                try:
                    if account_cache_key == tuple(cached.get("account_cache_key", ())):
                        for ws_dict in cached["ws_info"]:
                            for package_info in ws_dict["packages"]:
                                key = (package_info["workspaceId"], package_info["id"])
                                sort_key = "%05d%s" % (
                                    ws_id_and_pack_id_to_lru_index.get(
                                        key, DEFAULT_SORT_KEY
                                    ),
                                    package_info["name"].lower(),
                                )

                                package_info["sortKey"] = sort_key
                        return {
                            "success": True,
                            "message": None,
                            "result": cached["ws_info"],
                        }
                except Exception:
                    log.exception(
                        "Error computing new sort keys for cached entry. Refreshing and proceeding."
                    )

        last_error_result = None

        with progress_context(
            self._endpoint, "Listing cloud workspaces", self._dir_cache
        ):
            ws: IRccWorkspace
            ret: List[WorkspaceInfoDict] = []
            result = self._rcc.cloud_list_workspaces()
            if not result.success:
                return result.as_dict()

            workspaces = result.result
            for ws in workspaces:
                packages: List[PackageInfoDict] = []

                activity_package: IRccRobotMetadata
                activities_result = self._rcc.cloud_list_workspace_robots(
                    ws.workspace_id
                )
                if not activities_result.success:
                    # If we can't list the robots of a specific workspace, just skip it
                    # (the log should still show it but we can proceed to list the
                    # contents of other workspaces).
                    last_error_result = activities_result
                    continue

                workspace_activities = activities_result.result
                for activity_package in workspace_activities:

                    key = (ws.workspace_id, activity_package.robot_id)
                    sort_key = "%05d%s" % (
                        ws_id_and_pack_id_to_lru_index.get(key, DEFAULT_SORT_KEY),
                        activity_package.robot_name.lower(),
                    )

                    package_info = {
                        "name": activity_package.robot_name,
                        "id": activity_package.robot_id,
                        "sortKey": sort_key,
                        "workspaceId": ws.workspace_id,
                        "workspaceName": ws.workspace_name,
                    }
                    packages.append(package_info)

                ws_dict = {
                    "workspaceName": ws.workspace_name,
                    "workspaceId": ws.workspace_id,
                    "packages": packages,
                }
                ret.append(ws_dict)

        if not ret and last_error_result is not None:
            return last_error_result.as_dict()

        if ret:  # Only store if we got something.
            store: ListWorkspaceCachedInfoDict = {
                "ws_info": ret,
                "account_cache_key": account_cache_key,
            }
            self._dir_cache.store(self.CLOUD_LIST_WORKSPACE_CACHE_KEY, store)
        return {"success": True, "message": None, "result": ret}

    @command_dispatcher(commands.ROBOCORP_CREATE_ROBOT_INTERNAL)
    def _create_robot(self, params: CreateRobotParamsDict) -> ActionResultDict:
        self._feedback_metric("vscode.create.robot")
        directory = params["directory"]
        template = params["template"]
        name = params["name"]

        return self._rcc.create_robot(template, os.path.join(directory, name)).as_dict()

    @command_dispatcher(commands.ROBOCORP_LIST_ROBOT_TEMPLATES_INTERNAL)
    def _list_activity_templates(self, params=None) -> ActionResultDict:
        result = self._rcc.get_template_names()
        return result.as_dict()

    def _get_robot_metadata(
        self,
        sub: Path,
        curr_cache: Dict[Path, CachedFileInfo[LocalRobotMetadataInfoDict]],
        new_cache: Dict[Path, CachedFileInfo[LocalRobotMetadataInfoDict]],
    ) -> Optional[LocalRobotMetadataInfoDict]:
        """
        Note that we get the value from the current cache and then put it in
        the new cache if it's still valid (that way we don't have to mutate
        the old cache to remove stale values... all that's valid is put in
        the new cache).
        """
        robot_yaml = sub / "robot.yaml"

        cached_file_info: Optional[
            CachedFileInfo[LocalRobotMetadataInfoDict]
        ] = curr_cache.get(sub)
        if cached_file_info is not None:
            if cached_file_info.is_cache_valid():
                new_cache[sub] = cached_file_info
                return cached_file_info.value

        if robot_yaml.exists():
            from robocorp_ls_core import yaml_wrapper

            try:

                def get_robot_metadata(robot_yaml: Path):
                    name = robot_yaml.parent.name
                    with robot_yaml.open("r", encoding="utf-8") as stream:
                        yaml_contents = yaml_wrapper.load(stream)
                        name = yaml_contents.get("name", name)

                    robot_metadata: LocalRobotMetadataInfoDict = {
                        "directory": str(sub),
                        "filePath": str(robot_yaml),
                        "name": name,
                        "yamlContents": yaml_contents,
                    }
                    return robot_metadata

                cached_file_info = new_cache[sub] = CachedFileInfo(
                    robot_yaml, get_robot_metadata
                )
                return cached_file_info.value

            except:
                log.exception(f"Unable to get load robot metadata for: {robot_yaml}")

        return None

    @command_dispatcher(commands.ROBOCORP_RUN_IN_RCC_INTERNAL)
    def _run_in_rcc_internal(self, params=RunInRccParamsDict) -> ActionResultDict:
        try:
            args = params["args"]
            ret = self._rcc._run_rcc(args)
        except Exception as e:
            log.exception(f"Error running in RCC: {params}.")
            return dict(success=False, message=str(e), result=None)
        return ret.as_dict()

    @command_dispatcher(commands.ROBOCORP_LOCAL_LIST_ROBOTS_INTERNAL)
    def _local_list_robots(self, params=None) -> ActionResultDictLocalRobotMetadata:
        curr_cache = self._local_list_robots_cache
        new_cache: Dict[Path, CachedFileInfo[LocalRobotMetadataInfoDict]] = {}

        ret: List[LocalRobotMetadataInfoDict] = []
        try:
            ws = self.workspace
            if ws:
                for folder_path in ws.get_folder_paths():
                    # Check the root directory itself for the robot.yaml.
                    p = Path(folder_path)
                    robot_metadata = self._get_robot_metadata(p, curr_cache, new_cache)
                    if robot_metadata is not None:
                        ret.append(robot_metadata)
                    elif p.is_dir():
                        for sub in p.iterdir():
                            robot_metadata = self._get_robot_metadata(
                                sub, curr_cache, new_cache
                            )
                            if robot_metadata is not None:
                                ret.append(robot_metadata)

            ret.sort(key=lambda dct: dct["name"])
        except Exception as e:
            log.exception("Error listing robots.")
            return dict(success=False, message=str(e), result=None)
        finally:
            # Set the new cache after we finished computing all entries.
            self._local_list_robots_cache = new_cache

        return dict(success=True, message=None, result=ret)

    def _validate_directory(self, directory) -> Optional[str]:
        if not os.path.exists(directory):
            return f"Expected: {directory} to exist."

        if not os.path.isdir(directory):
            return f"Expected: {directory} to be a directory."
        return None

    def _add_package_info_to_access_lru(self, workspace_id, package_id, directory):
        import time

        try:
            lst: List[PackageInfoInLRUDict] = self._dir_cache.load(
                self.PACKAGE_ACCESS_LRU_CACHE_KEY, list
            )
        except KeyError:
            lst = []

        new_lst: List[PackageInfoInLRUDict] = [
            {
                "workspace_id": workspace_id,
                "package_id": package_id,
                "directory": directory,
                "time": time.time(),
            }
        ]
        for i, entry in enumerate(lst):
            if isinstance(entry, dict):
                if (
                    entry.get("package_id") == package_id
                    and entry.get("workspace_id") == workspace_id
                ):
                    continue  # Skip this one (we moved it to the start of the LRU).
                new_lst.append(entry)

            if i == 5:
                break  # Max number of items in the LRU reached.

        self._dir_cache.store(self.PACKAGE_ACCESS_LRU_CACHE_KEY, new_lst)

    @command_dispatcher(commands.ROBOCORP_UPLOAD_TO_EXISTING_ROBOT_INTERNAL)
    def _upload_to_existing_activity(
        self, params: UploadRobotParamsDict
    ) -> ActionResultDict:
        from robocorp_ls_core.progress_report import progress_context

        self._feedback_metric("vscode.cloud.upload.existing")

        directory = params["directory"]
        error_msg = self._validate_directory(directory)
        if error_msg:
            return {"success": False, "message": error_msg, "result": None}

        workspace_id = params["workspaceId"]
        robot_id = params["robotId"]
        with progress_context(
            self._endpoint, "Uploading to existing robot", self._dir_cache
        ):

            result = self._rcc.cloud_set_robot_contents(
                directory, workspace_id, robot_id
            )
            self._add_package_info_to_access_lru(workspace_id, robot_id, directory)

        return result.as_dict()

    @command_dispatcher(commands.ROBOCORP_UPLOAD_TO_NEW_ROBOT_INTERNAL)
    def _upload_to_new_robot(
        self, params: UploadNewRobotParamsDict
    ) -> ActionResultDict:
        from robocorp_ls_core.progress_report import progress_context

        self._feedback_metric("vscode.cloud.upload.new")

        directory = params["directory"]
        error_msg = self._validate_directory(directory)
        if error_msg:
            return {"success": False, "message": error_msg, "result": None}

        workspace_id = params["workspaceId"]
        robot_name = params["robotName"]

        # When we upload to a new activity, clear the existing cache key.
        self._dir_cache.discard(self.CLOUD_LIST_WORKSPACE_CACHE_KEY)
        with progress_context(
            self._endpoint, "Uploading to new robot", self._dir_cache
        ):
            new_robot_result = self._rcc.cloud_create_robot(workspace_id, robot_name)
            if not new_robot_result.success:
                return new_robot_result.as_dict()

            robot_id = new_robot_result.result
            if not robot_id:
                return dict(
                    success=False,
                    message="Expected to have package id from creating new activity.",
                    result=None,
                )

            result = self._rcc.cloud_set_robot_contents(
                directory, workspace_id, robot_id
            )
            self._add_package_info_to_access_lru(workspace_id, robot_id, directory)
        return result.as_dict()

    @command_dispatcher(commands.ROBOCORP_GET_PLUGINS_DIR, str)
    def _get_plugins_dir(self, params=None) -> str:
        return str(Path(__file__).parent / "plugins")

    @command_dispatcher(
        commands.ROBOCORP_COMPUTE_ROBOT_LAUNCH_FROM_ROBOCORP_CODE_LAUNCH
    )
    def _compute_robot_launch_from_robocorp_code_launch(
        self, params: dict
    ) -> ActionResultDictRobotLaunch:
        from robocorp_code import compute_launch

        name: Optional[str] = params.get("name")
        request: Optional[str] = params.get("request")
        task: Optional[str] = params.get("task")
        robot: Optional[str] = params.get("robot")
        additional_pythonpath_entries: Optional[List[str]] = params.get(
            "additionalPythonpathEntries"
        )
        env: Optional[Dict[str, str]] = params.get("env")
        python_exe: Optional[str] = params.get("pythonExe")

        return compute_launch.compute_robot_launch_from_robocorp_code_launch(
            name, request, task, robot, additional_pythonpath_entries, env, python_exe
        )

    @command_dispatcher(commands.ROBOCORP_RESOLVE_INTERPRETER, dict)
    def _resolve_interpreter(self, params=None) -> ActionResultDict:
        from robocorp_ls_core.ep_resolve_interpreter import EPResolveInterpreter
        from robocorp_ls_core.ep_resolve_interpreter import IInterpreterInfo

        try:
            from robocorp_ls_core import uris

            target_robot: str = params.get("target_robot")

            for ep in self._pm.get_implementations(EPResolveInterpreter):
                interpreter_info: IInterpreterInfo = (
                    ep.get_interpreter_info_for_doc_uri(uris.from_fs_path(target_robot))
                )
                if interpreter_info is not None:
                    return {
                        "success": True,
                        "message": None,
                        "result": {
                            "pythonExe": interpreter_info.get_python_exe(),
                            "environ": interpreter_info.get_environ(),
                            "additionalPythonpathEntries": interpreter_info.get_additional_pythonpath_entries(),
                        },
                    }
        except Exception as e:
            log.exception(f"Error resolving interpreter. Args: {params}")
            return {"success": False, "message": str(e), "result": None}

        # i.e.: no error but we couldn't find an interpreter.
        return {"success": True, "message": "", "result": None}

    @command_dispatcher(
        commands.ROBOCORP_START_BROWSER_LOCATOR_INTERNAL, expected_return_cls=Future
    )
    def _start_browser_locator(
        self, params: dict = None
    ) -> "Union[ActionResultDict, Future[ActionResultDict]]":
        if not params or "robotYaml" not in params:
            self._locators_in_thread_api.set_robot_yaml_location("")
            return {
                "success": False,
                "message": "robot.yaml filename not passed",
                "result": None,
            }

        robot_yaml = params["robotYaml"]
        if not os.path.exists(robot_yaml):
            return {
                "success": False,
                "message": f"robot.yaml {robot_yaml} does not exist.",
                "result": None,
            }

        return self._locators_in_thread_api.start_browser_locator(robot_yaml=robot_yaml)

    @command_dispatcher(
        commands.ROBOCORP_STOP_BROWSER_LOCATOR, expected_return_cls=Future
    )
    def _stop_browser_locator(self, params: dict = None) -> "Future[ActionResultDict]":
        return self._locators_in_thread_api.browser_locator_stop()

    @command_dispatcher(
        commands.ROBOCORP_CREATE_LOCATOR_FROM_BROWSER_PICK_INTERNAL,
        expected_return_cls=Future,
    )
    def _create_locator_fom_browser_pick(
        self, params: dict = None
    ) -> "Future[ActionResultDict]":
        return self._locators_in_thread_api.create_locator_from_browser_pick()

    def m_text_document__hover(self, **kwargs):
        """
        When hovering over a png in base64 surrounded by double-quotes... something as:
        "iVBORw0KGgo...rest of png in base 64 contents..."
        
        i.e.: Provide the contents in markdown format to show the actual image from the
        locators.json.
        """
        from robocorp_ls_core import uris
        from robocorp_ls_core.protocols import IDocument
        from robocorp_ls_core.protocols import IDocumentSelection
        from robocorp_ls_core.lsp import Range
        from robocorp_ls_core.lsp import MarkupKind
        from robocorp_ls_core.lsp import MarkupContent

        doc_uri = kwargs["textDocument"]["uri"]
        # Note: 0-based
        line: int = kwargs["position"]["line"]
        col: int = kwargs["position"]["character"]
        if not uris.to_fs_path(doc_uri).endswith("locators.json"):
            return None
        document: IDocument = self._workspace.get_document(
            doc_uri, accept_from_file=True
        )
        sel: IDocumentSelection = document.selection(line, col)
        current_line: str = sel.current_line
        i: int = current_line.find(
            '"iVBORw0KGgo'
        )  # I.e.: pngs in base64 always start with this prefix.
        if i >= 0:
            current_line = current_line[i + 1 :]
            i = current_line.find('"')
            if i >= 0:
                current_line = current_line[0:i]
                image_path = f"data:image/png;base64,{current_line}"
                s = f"![Screenshot]({image_path})"
                return {
                    "contents": MarkupContent(MarkupKind.Markdown, s).to_dict(),
                    "range": Range((line, col), (line, col)).to_dict(),
                }

        return None
