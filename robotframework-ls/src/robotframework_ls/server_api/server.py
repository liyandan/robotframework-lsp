from robocorp_ls_core.python_ls import PythonLanguageServer
from robocorp_ls_core.basic import overrides
from robocorp_ls_core.robotframework_log import get_logger
from typing import Optional, List
from robocorp_ls_core.protocols import IConfig, IMonitor
from functools import partial
from robocorp_ls_core.jsonrpc.endpoint import require_monitor
from robocorp_ls_core.lsp import SymbolInformationTypedDict


log = get_logger(__name__)


class RobotFrameworkServerApi(PythonLanguageServer):
    """
    This is a custom server. It uses the same message-format used in the language
    server but with custom messages (i.e.: this is not the language server, but
    an API to use the bits we need from robotframework in a separate process).
    """

    def __init__(self, read_from, write_to, libspec_manager=None):
        from robotframework_ls.impl.libspec_manager import LibspecManager

        if libspec_manager is None:
            try:
                libspec_manager = LibspecManager()
            except:
                log.exception("Unable to properly initialize the LibspecManager.")
                raise

        self.libspec_manager = libspec_manager
        PythonLanguageServer.__init__(self, read_from, write_to)
        self._version = None

    @overrides(PythonLanguageServer._create_config)
    def _create_config(self) -> IConfig:
        from robotframework_ls.robot_config import RobotConfig

        return RobotConfig()

    def m_version(self):
        if self._version is not None:
            return self._version
        try:
            import robot  # noqa
        except:
            log.exception("Unable to import 'robot'.")
            version = "NO_ROBOT"
        else:
            try:
                from robot import get_version

                version = get_version(naked=True)
            except:
                log.exception("Unable to get version.")
                version = "N/A"  # Too old?
        self._version = version
        return self._version

    def _check_min_version(self, min_version):
        from robocorp_ls_core.basic import check_min_version

        version = self.m_version()
        return check_min_version(version, min_version)

    @overrides(PythonLanguageServer.m_workspace__did_change_configuration)
    def m_workspace__did_change_configuration(self, **kwargs):
        PythonLanguageServer.m_workspace__did_change_configuration(self, **kwargs)
        self.libspec_manager.config = self.config

    @overrides(PythonLanguageServer.lint)
    def lint(self, *args, **kwargs):
        pass  # No-op for this server.

    @overrides(PythonLanguageServer.cancel_lint)
    def cancel_lint(self, *args, **kwargs):
        pass  # No-op for this server.

    @overrides(PythonLanguageServer._create_workspace)
    def _create_workspace(self, root_uri, workspace_folders):
        from robotframework_ls.impl.robot_workspace import RobotWorkspace

        return RobotWorkspace(
            root_uri, workspace_folders, libspec_manager=self.libspec_manager
        )

    def m_lint(self, doc_uri):
        if not self._check_min_version((3, 2)):
            from robocorp_ls_core.lsp import Error

            msg = (
                "robotframework version (%s) too old for linting.\n"
                "Please install a newer version and restart the language server."
                % (self.m_version(),)
            )
            log.info(msg)
            return [Error(msg, (0, 0), (1, 0)).to_lsp_diagnostic()]

        func = partial(self._threaded_lint, doc_uri)
        func = require_monitor(func)
        return func

    def _threaded_lint(self, doc_uri, monitor: IMonitor):
        from robocorp_ls_core.jsonrpc.exceptions import JsonRpcRequestCancelled

        try:
            from robotframework_ls.impl.ast_utils import collect_errors
            from robotframework_ls.impl import code_analysis

            log.debug("Lint: starting (in thread).")

            completion_context = self._create_completion_context(doc_uri, 0, 0, monitor)
            if completion_context is None:
                return []

            ast = completion_context.get_ast()
            monitor.check_cancelled()
            errors = collect_errors(ast)
            log.debug("Collected AST errors (in thread): %s", len(errors))
            monitor.check_cancelled()
            analysis_errors = code_analysis.collect_analysis_errors(completion_context)
            log.debug("Collected analysis errors (in thread): %s", len(analysis_errors))
            errors.extend(analysis_errors)
            return [error.to_lsp_diagnostic() for error in errors]
        except JsonRpcRequestCancelled:
            raise JsonRpcRequestCancelled("Lint cancelled (inside lint)")
        except:
            log.exception("Error collecting errors.")
            return []

    def m_complete_all(self, doc_uri, line, col):
        func = partial(self._threaded_complete_all, doc_uri, line, col)
        func = require_monitor(func)
        return func

    def _threaded_complete_all(self, doc_uri, line, col, monitor: IMonitor):
        from robotframework_ls.impl import section_name_completions
        from robotframework_ls.impl import keyword_completions
        from robotframework_ls.impl import variable_completions
        from robotframework_ls.impl import filesystem_section_completions
        from robotframework_ls.impl import keyword_parameter_completions

        completion_context = self._create_completion_context(
            doc_uri, line, col, monitor
        )
        if completion_context is None:
            return []

        ret = section_name_completions.complete(completion_context)
        if not ret:
            ret.extend(filesystem_section_completions.complete(completion_context))

        if not ret:
            ret.extend(keyword_completions.complete(completion_context))

        if not ret:
            ret.extend(variable_completions.complete(completion_context))

        if not ret:
            ret.extend(keyword_parameter_completions.complete(completion_context))

        return ret

    def m_section_name_complete(self, doc_uri, line, col):
        from robotframework_ls.impl import section_name_completions

        completion_context = self._create_completion_context(doc_uri, line, col, None)
        if completion_context is None:
            return []

        return section_name_completions.complete(completion_context)

    def m_keyword_complete(self, doc_uri, line, col):
        from robotframework_ls.impl import keyword_completions

        completion_context = self._create_completion_context(doc_uri, line, col, None)
        if completion_context is None:
            return []
        return keyword_completions.complete(completion_context)

    def m_find_definition(self, doc_uri, line, col):
        func = partial(self._threaded_find_definition, doc_uri, line, col)
        func = require_monitor(func)
        return func

    def _threaded_find_definition(self, doc_uri, line, col, monitor) -> Optional[list]:
        from robotframework_ls.impl.find_definition import find_definition
        import os.path
        from robocorp_ls_core.lsp import Location, Range
        from robocorp_ls_core import uris

        completion_context = self._create_completion_context(
            doc_uri, line, col, monitor
        )
        if completion_context is None:
            return None
        definitions = find_definition(completion_context)
        ret = []
        for definition in definitions:
            if not definition.source:
                log.info("Found definition with empty source (%s).", definition)
                continue

            if not os.path.exists(definition.source):
                log.info(
                    "Found definition: %s (but source does not exist).", definition
                )
                continue

            lineno = definition.lineno
            if lineno is None or lineno < 0:
                lineno = 0

            end_lineno = definition.end_lineno
            if end_lineno is None or end_lineno < 0:
                end_lineno = 0

            col_offset = definition.col_offset
            end_col_offset = definition.end_col_offset

            ret.append(
                Location(
                    uris.from_fs_path(definition.source),
                    Range((lineno, col_offset), (end_lineno, end_col_offset)),
                ).to_dict()
            )
        return ret

    def m_code_format(self, text_document, options):
        func = partial(self._threaded_code_format, text_document, options)
        func = require_monitor(func)
        return func

    def _threaded_code_format(self, text_document, options, monitor: IMonitor):
        from robotframework_ls.impl.formatting import robot_source_format
        from robotframework_ls.impl.formatting import create_text_edit_from_diff
        from robocorp_ls_core.lsp import TextDocumentItem

        text_document_item = TextDocumentItem(**text_document)
        text = text_document_item.text
        if not text:
            completion_context = self._create_completion_context(
                text_document_item.uri, 0, 0, monitor
            )
            if completion_context is None:
                return []
            text = completion_context.doc.source

        if not text:
            return []

        if options is None:
            options = {}
        tab_size = options.get("tabSize", 4)

        new_contents = robot_source_format(text, space_count=tab_size)
        if new_contents is None or new_contents == text:
            return []
        return [x.to_dict() for x in create_text_edit_from_diff(text, new_contents)]

    def _create_completion_context(self, doc_uri, line, col, monitor: IMonitor):
        from robotframework_ls.impl.completion_context import CompletionContext

        if not self._check_min_version((3, 2)):
            log.info("robotframework version too old.")
            return None
        workspace = self.workspace
        if not workspace:
            log.info("Workspace still not initialized.")
            return None

        document = workspace.get_document(doc_uri, accept_from_file=True)
        if document is None:
            log.info("Unable to get document for uri: %s.", doc_uri)
            return None
        return CompletionContext(
            document,
            line,
            col,
            workspace=workspace,
            config=self.config,
            monitor=monitor,
        )

    def m_signature_help(self, doc_uri: str, line: int, col: int):
        func = partial(self._threaded_signature_help, doc_uri, line, col)
        func = require_monitor(func)
        return func

    def _threaded_signature_help(
        self, doc_uri: str, line: int, col: int, monitor: IMonitor
    ) -> Optional[dict]:
        from robotframework_ls.impl.signature_help import signature_help

        completion_context = self._create_completion_context(
            doc_uri, line, col, monitor
        )
        if completion_context is None:
            return None

        return signature_help(completion_context)

    def m_workspace_symbols(self, query: Optional[str] = None):
        func = partial(self._threaded_workspace_symbols, query)
        func = require_monitor(func)
        return func

    def _threaded_workspace_symbols(
        self, query: Optional[str], monitor: IMonitor
    ) -> Optional[List[SymbolInformationTypedDict]]:
        from robotframework_ls.impl.workspace_symbols import workspace_symbols
        from robotframework_ls.impl.completion_context import BaseContext
        from robotframework_ls.impl.protocols import IRobotWorkspace
        from typing import cast

        workspace = self._workspace
        if not workspace:
            return []

        robot_workspace = cast(IRobotWorkspace, workspace)

        return workspace_symbols(
            query,
            BaseContext(workspace=robot_workspace, config=self.config, monitor=monitor),
        )

    def m_shutdown(self, **_kwargs):
        PythonLanguageServer.m_shutdown(self, **_kwargs)
        self.libspec_manager.dispose()

    def m_exit(self, **_kwargs):
        PythonLanguageServer.m_exit(self, **_kwargs)
        self.libspec_manager.dispose()
