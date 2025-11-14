# TODO: Validate that users don't select overlapping paths...
# TODO: Maybe add session deleted labels which show how many files and their size deleted this session?
# TODO: Add a general "process options" button to delete, ignore, move, etc etc
# TODO: Add keep zip, grup, column, folder
# TODO: Enable ignore folder before release!


import logging
import shutil

from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from PySide6 import QtCore, QtGui, QtWidgets

from pixmatch import Finished, ImageMatch, ImageMatcher, NewGroup, NewMatch, ZipPath, _is_under
from pixmatch.gui.utils import MAX_SIZE_POLICY, NO_MARGIN
from pixmatch.gui.widgets import (
    STATE_ORDER,
    DirFileSystemModel,
    DuplicateGroupList,
    ImageViewPane,
    SelectionState,
    SelectionValue,
    SelectionValues,
    _prompt_move_clear,
)
from pixmatch.utils import human_bytes

ICON_PATH = Path(__file__).resolve().parent / 'pixmatch.ico'

logger = logging.getLogger(__name__)


def ceildiv(a, b):
    """The opposite of floordiv, //"""
    return -(a // -b)


def project_version() -> str:
    """Get the version string to display in the title bar"""
    try:
        return version("pixmatch")
    except PackageNotFoundError:
        return "0.0.0+unknown"


class WorkerSignals(QtCore.QObject):
    """
    Signals from a running worker thread.

    new_group
        Signaled when a new match group is found.

    new_match
        Signaled when a new match is found.

    finish
        Signaled when execution is finished, and was not killed.
    """

    new_group = QtCore.Signal(object)
    new_match = QtCore.Signal(tuple)
    finish = QtCore.Signal()


class ProcessorThread(QtCore.QRunnable):
    """Handles executing the processor and transferring data from the processor to the GUI"""

    def __init__(self, processor, *args, **kwargs):
        super().__init__()
        self.args = args
        self.kwargs = kwargs
        self.processor = processor
        self.signals = WorkerSignals()

        # timer lives on the GUI thread; it polls the library queue
        self._poller = QtCore.QTimer()
        self._poller.setInterval(250)
        self._poller.timeout.connect(self._drain_events)
        self._poller.start()

    def _drain_events(self):
        """Take events from the queue and forward them to the signals"""
        while not self.processor.events.empty():
            evt = self.processor.events.get_nowait()
            if isinstance(evt, NewGroup):
                self.signals.new_group.emit(evt.group)
            elif isinstance(evt, NewMatch):
                self.signals.new_match.emit((evt.group, evt.path))
            elif isinstance(evt, Finished):
                self._poller.stop()
                self.signals.finish.emit()

    def run(self):
        """Execute the processor"""
        self.processor.run(*self.args, **self.kwargs)


class MainWindow(QtWidgets.QMainWindow):
    """
    The main application window emulating VisiPics' primary workflow.

    Key elements:
      - Toolbar with folder selection (stub), Load Test Image, Similarity slider
      - Central area that shows one group/page at a time
      - Prev/Next page navigation
    """

    def __init__(self, start_paths=None):
        super().__init__()
        self.setWindowTitle(f"PixMatch v{project_version()}")
        self.resize(1200, 800)

        # State
        self.current_page: int = 1
        self.processor = None
        self.file_states = {}
        self._threadpool = QtCore.QThreadPool()

        # UI build
        self.build_menubar()
        self.build_central()
        self.build_statusbar()
        self.build_extra()

        for start_path in start_paths or []:
            self.selected_file_path_display.addItem(str(start_path))

        if start_paths:
            self.selected_file_path_display.setCurrentRow(0)

        self.setWindowIcon(QtGui.QIcon(QtGui.QPixmap(ICON_PATH)))

    def build_extra(self):
        """Extra items not involved with the main display"""
        self.exit_warning = QtWidgets.QMessageBox(self)
        self.exit_warning.setIcon(QtWidgets.QMessageBox.Icon.Warning)
        self.exit_warning.setWindowTitle("Close?")
        self.exit_warning.setText("Are you sure you want to quit?")
        self.exit_warning.setStandardButtons(
            QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No,
        )
        self.exit_warning.setDefaultButton(QtWidgets.QMessageBox.StandardButton.No)
        self.exit_warning.setEscapeButton(QtWidgets.QMessageBox.StandardButton.No)
        self.exit_warning.setWindowModality(QtCore.Qt.WindowModality.ApplicationModal)

    def build_menubar(self) -> None:
        """Creates the top menu bar"""
        menu = self.menuBar()

        # region File menu
        load_project = QtGui.QAction("Load Project...", self, enabled=False)
        save_project = QtGui.QAction("Save Project...", self, enabled=False)
        exit_project = QtGui.QAction("Exit", self)
        exit_project.triggered.connect(lambda *_: self.close())

        file_menu = menu.addMenu("&File")
        file_menu.addAction(load_project)
        file_menu.addAction(save_project)
        file_menu.addSeparator()
        file_menu.addAction(exit_project)
        # endregion

        # region Edit menu
        # TODO: When adding this, make sure not to allow marking a zip file as delete
        self.mark_delete_menu = QtGui.QAction("Delete", self)
        self.mark_delete_group_menu = QtGui.QAction("Delete Group", self)
        mark_ignore = QtGui.QAction("Ignore", self)
        mark_ignore_group = QtGui.QAction("Ignore Group", self)
        mark_ignore_folder = QtGui.QAction("Ignore Folder", self, enabled=False)
        self.mark_ignore_zip_menu = QtGui.QAction("Ignore Zip", self)
        self.mark_move_menu = QtGui.QAction("Move this file...", self)
        mark_symlink = QtGui.QAction("Symlink this file...", self, enabled=False)
        unmark = QtGui.QAction("Un-select", self)

        self.mark_delete_menu.triggered.connect(self.mark_delete)
        self.mark_delete_group_menu.triggered.connect(self.mark_delete_group)
        self.mark_move_menu.triggered.connect(self.mark_move)
        mark_ignore.triggered.connect(self.mark_ignore)
        mark_ignore_group.triggered.connect(self.mark_ignore_group)
        self.mark_ignore_zip_menu.triggered.connect(self.mark_ignore_zip)
        unmark.triggered.connect(self.mark_unmark)

        edit_menu = menu.addMenu("&Edit")
        edit_menu.addAction(self.mark_delete_menu)
        edit_menu.addAction(self.mark_delete_group_menu)
        edit_menu.addSeparator()
        edit_menu.addAction(mark_ignore)
        edit_menu.addAction(mark_ignore_group)
        edit_menu.addAction(mark_ignore_folder)
        edit_menu.addAction(self.mark_ignore_zip_menu)
        edit_menu.addSeparator()
        edit_menu.addAction(self.mark_move_menu)
        edit_menu.addAction(mark_symlink)
        edit_menu.addSeparator()
        edit_menu.addAction(unmark)
        # endregion

        # region View menu
        page_next = QtGui.QAction("Next page", self)
        page_next.triggered.connect(self.on_page_up)
        page_back = QtGui.QAction("Previous page", self)
        page_back.triggered.connect(self.on_page_down)
        self.preview_resized = QtGui.QAction("Preview resized", self, checked=True, checkable=True)
        preview_full_size = QtGui.QAction("Preview full size", self, checkable=True)

        preview_options_grp = QtGui.QActionGroup(self)
        preview_options_grp.setExclusive(True)
        preview_options_grp.addAction(preview_full_size)
        preview_options_grp.addAction(self.preview_resized)

        # TODO: I'm not sure why we would want to slow display?
        #   and show differences has never worked for me
        # QtGui.QAction("Slow preview display", self)
        # QtGui.QAction("Show differences", self)

        view_menu = menu.addMenu("&View")
        view_menu.addAction(page_next)
        view_menu.addAction(page_back)
        view_menu.addSeparator()
        view_menu.addAction(self.preview_resized)
        view_menu.addAction(preview_full_size)
        # endregion

        # region Tools menu
        autoselect = QtGui.QAction("Auto-select", self)
        autoselect.setEnabled(False)  # TODO:

        tool_menu = menu.addMenu("&Tools")
        tool_menu.addAction(autoselect)
        # endregion

        # region Actions menu
        run_move = QtGui.QAction("Move", self)
        run_move.triggered.connect(self.on_move)
        run_delete = QtGui.QAction("Delete", self)
        run_delete.triggered.connect(self.on_delete)
        run_ignore = QtGui.QAction("Save ignored pictures", self)
        run_ignore.triggered.connect(self.on_ignore)

        actions_menu = menu.addMenu("&Actions")
        actions_menu.addAction(run_move)
        actions_menu.addAction(run_delete)
        actions_menu.addAction(run_ignore)
        # endregion

        # region Options menu
        option_hidden_folders = QtGui.QAction("Show hidden folders", self, checkable=True, enabled=False)
        option_subfolders = QtGui.QAction("Include subfolders", self, checkable=True, checked=True, enabled=False)
        option_rotations = QtGui.QAction("Scan for rotations", self, checkable=True, checked=True, enabled=False)

        # TODO: I'm not sure what these two do... I'm willing to add them if someone needs them but I don't.
        # ... = QtGui.QAction("Between folders only", self)
        # ... = QtGui.QAction("Loosen filter automatically", self)

        options_menu = menu.addMenu("&Options")
        options_menu.addAction(option_hidden_folders)
        options_menu.addAction(option_subfolders)
        options_menu.addSeparator()
        options_menu.addAction(option_rotations)
        # endregion

    def build_central(self) -> None:
        """Creates the central stacked widget area for group pages."""
        style = QtWidgets.QApplication.instance().style()

        # region General controls area (top-right)
        # region Control buttons
        autoselect_btn = QtWidgets.QPushButton("Auto-select")
        autoselect_btn.setEnabled(False)  # TODO:

        tools = QtWidgets.QVBoxLayout()
        tools.addWidget(autoselect_btn)

        tool_box = QtWidgets.QGroupBox("Tools")
        tool_box.setLayout(tools)
        tool_box.setMaximumHeight(60)

        move_btn = QtWidgets.QPushButton("Move")
        move_btn.pressed.connect(self.on_move)
        delete_btn = QtWidgets.QPushButton("Delete")
        delete_btn.pressed.connect(self.on_delete)
        save_ignored_btn = QtWidgets.QPushButton("Save ignored")
        save_ignored_btn.pressed.connect(self.on_ignore)

        actions = QtWidgets.QVBoxLayout()
        actions.addWidget(move_btn)
        actions.addWidget(delete_btn)
        actions.addWidget(save_ignored_btn)

        actions_box = QtWidgets.QGroupBox("Actions")
        actions_box.setLayout(actions)
        actions_box.setMaximumHeight(20 + 40 * 3)

        general_controls_btns = QtWidgets.QVBoxLayout()
        general_controls_btns.setAlignment(QtCore.Qt.AlignmentFlag.AlignTop)
        general_controls_btns.setContentsMargins(NO_MARGIN)
        general_controls_btns.addWidget(tool_box)
        general_controls_btns.addWidget(actions_box)
        # endregion

        # region Run Controls
        self.stop_btn = QtWidgets.QPushButton("\u25A0")
        self.stop_btn.setSizePolicy(MAX_SIZE_POLICY)
        self.stop_btn.setCheckable(True)
        self.stop_btn.setChecked(True)
        self.stop_btn.setStyleSheet('QPushButton {font-size: 26pt; color: maroon;}')
        self.stop_btn.clicked.connect(self.on_pause)

        self.start_btn = QtWidgets.QPushButton("\u25B6")
        self.start_btn.setSizePolicy(MAX_SIZE_POLICY)
        self.start_btn.setCheckable(True)
        self.start_btn.setStyleSheet('QPushButton {font-size: 32pt; color: green;}')
        self.start_btn.clicked.connect(self.on_start)

        self.pause_btn = QtWidgets.QPushButton()
        self.pause_btn.setSizePolicy(MAX_SIZE_POLICY)
        self.pause_btn.setCheckable(True)
        self.pause_btn.setEnabled(False)
        self.pause_btn.clicked.connect(self.on_pause)
        self.pause_btn.setIcon(style.standardIcon(
            QtWidgets.QStyle.StandardPixmap.SP_MediaPause,
        ))
        self.pause_btn.setIconSize(QtCore.QSize(28, 28))

        run_controls_options_grp = QtWidgets.QButtonGroup(self, exclusive=True)
        run_controls_options_grp.addButton(self.stop_btn)
        run_controls_options_grp.addButton(self.start_btn)
        run_controls_options_grp.addButton(self.pause_btn)

        run_controls = QtWidgets.QHBoxLayout()
        run_controls.setContentsMargins(NO_MARGIN)
        run_controls.addWidget(self.stop_btn)
        run_controls.addWidget(self.start_btn)
        run_controls.addWidget(self.pause_btn)
        # endregion

        labels = QtWidgets.QVBoxLayout()
        labels.setContentsMargins(NO_MARGIN)
        self._remaining_files_label = QtWidgets.QLabel()
        self.set_remaining_files_label(0)
        self._loaded_pictures_label = QtWidgets.QLabel()
        self.set_loaded_pictures_label(0)
        self._dup_pictures_label = QtWidgets.QLabel()
        self.set_duplicate_images_label(0)
        self._dup_groups_label = QtWidgets.QLabel()
        self.set_duplicate_groups_label(0)

        self._timer_label = QtWidgets.QLabel("00:00:00", alignment=QtCore.Qt.AlignmentFlag.AlignHCenter)
        self._elapsed_secs = 0
        self._run_timer = QtCore.QTimer(self)
        self._run_timer.setInterval(1000)  # 1s ticks
        self._run_timer.timeout.connect(self._on_run_timer_tick)

        self._label_timer = QtCore.QTimer(self)
        self._label_timer.setInterval(50)
        self._label_timer.timeout.connect(self._on_labels_tick)

        self._progress_bar = QtWidgets.QProgressBar(value=50, textVisible=False)
        labels.addWidget(self._remaining_files_label)
        labels.addWidget(self._loaded_pictures_label)
        labels.addWidget(self._dup_pictures_label)
        labels.addWidget(self._dup_groups_label)
        labels.addWidget(self._timer_label)
        labels.addWidget(self._progress_bar)
        labels.addLayout(run_controls)

        # region Settings tabs
        # TODO: Add other setting tabs

        # region Filter tab
        slider_labels = QtWidgets.QVBoxLayout()
        slider_labels.setContentsMargins(NO_MARGIN)
        slider_labels.addWidget(QtWidgets.QLabel(text="Strict", alignment=QtCore.Qt.AlignmentFlag.AlignTop))
        slider_labels.addWidget(QtWidgets.QLabel(text="Basic", alignment=QtCore.Qt.AlignmentFlag.AlignVCenter))
        slider_labels.addWidget(QtWidgets.QLabel(text="Loose", alignment=QtCore.Qt.AlignmentFlag.AlignBottom))

        self.precision_slider = QtWidgets.QSlider(tickPosition=QtWidgets.QSlider.TickPosition.TicksLeft)
        self.precision_slider.setMaximum(10)
        self.precision_slider.setValue(5)
        self.precision_slider.sliderMoved.connect(self.on_precision_adjust)

        filter_tab_main = QtWidgets.QHBoxLayout()
        filter_tab_main.setContentsMargins(NO_MARGIN)
        filter_tab_main.addLayout(slider_labels)
        filter_tab_main.addWidget(self.precision_slider)
        filter_tab_main.addWidget(QtWidgets.QLabel(
            text="The slider determines\n"
                 "how strictly the program\n"
                 "checks for similarities\n"
                 "between the images.\n"
                 "Strict means it checks if\n"
                 "an image is the same or\n"
                 "slightly different, loose\n"
                 "allows for a greater\n"
                 "amount of differences.",
            alignment=QtCore.Qt.AlignmentFlag.AlignCenter,
        ))

        filter_tab = QtWidgets.QVBoxLayout()
        self.hash_match_chkbx = QtWidgets.QCheckBox(" Hash match")
        self.hash_match_chkbx.setEnabled(False)
        filter_tab.addWidget(self.hash_match_chkbx)
        filter_tab.addLayout(filter_tab_main)
        # endregion

        such = QtWidgets.QTabWidget()
        such.addTab(QtWidgets.QWidget(layout=filter_tab), "Filter")
        # endregion

        labels_and_such = QtWidgets.QHBoxLayout()
        labels_and_such.setContentsMargins(NO_MARGIN)
        labels_and_such.addWidget(QtWidgets.QWidget(layout=labels, maximumWidth=140))
        labels_and_such.addWidget(such)

        primary_controls = QtWidgets.QVBoxLayout()
        primary_controls.setContentsMargins(NO_MARGIN)
        primary_controls.addLayout(self.build_file_path_selection_display())
        primary_controls.addWidget(QtWidgets.QWidget(layout=labels_and_such, fixedHeight=200))

        general_controls = QtWidgets.QHBoxLayout()
        general_controls.setContentsMargins(NO_MARGIN)
        general_controls.addLayout(primary_controls)
        general_controls.addWidget(QtWidgets.QWidget(layout=general_controls_btns, maximumWidth=130))

        # region File system explorer
        file_system_model = DirFileSystemModel()
        file_system_model.setFilter(QtCore.QDir.Filter.Dirs
                                    | QtCore.QDir.Filter.Drives
                                    | QtCore.QDir.Filter.NoDotAndDotDot)
        file_system_model.setRootPath("")
        self.file_system_view = QtWidgets.QTreeView(headerHidden=True)
        self.file_system_view.setContentsMargins(NO_MARGIN)
        self.file_system_view.setModel(file_system_model)
        self.file_system_view.setRootIndex(file_system_model.index(""))
        self.file_system_view.hideColumn(1)  # Size
        self.file_system_view.hideColumn(2)  # Type
        self.file_system_view.hideColumn(3)  # Date Modified
        file_view_splitter = QtWidgets.QHBoxLayout()
        file_view_splitter.setContentsMargins(NO_MARGIN)
        file_view_splitter.addWidget(self.file_system_view)
        file_view_splitter.addWidget(QtWidgets.QWidget(layout=general_controls, maximumWidth=600))
        # endregion
        # endregion

        outer_splitter = QtWidgets.QSplitter(QtCore.Qt.Orientation.Horizontal, childrenCollapsible=False)
        inner_splitter = QtWidgets.QSplitter(QtCore.Qt.Orientation.Vertical, childrenCollapsible=False)

        inner_splitter.addWidget(QtWidgets.QWidget(layout=file_view_splitter))
        inner_splitter.addWidget(self.build_image_view_area())
        inner_splitter.setSizes([250, 250])

        self.duplicate_group_list = DuplicateGroupList(sizePolicy=MAX_SIZE_POLICY)
        self.duplicate_group_list.groupTileStateChanged.connect(self.on_match_state_changed)
        self.duplicate_group_list.groupTileHovered.connect(self.on_tile_hover)
        self.duplicate_group_list.groupTileMove.connect(self.mark_move)
        self.duplicate_group_list.groupTileDeleteGroup.connect(self.mark_delete_group)
        self.duplicate_group_list.groupTileDeleteColumn.connect(self.mark_delete_column)
        self.duplicate_group_list.groupTileIgnoreGroup.connect(self.mark_ignore_group)
        self.duplicate_group_list.groupTileIgnoreColumn.connect(self.mark_ignore_column)
        self.duplicate_group_list.groupTileIgnoreZip.connect(self.mark_ignore_zip)
        self.duplicate_group_list.page_down.pressed.connect(self.on_page_down)
        self.duplicate_group_list.page_up.pressed.connect(self.on_page_up)
        self.duplicate_group_list.first_page.pressed.connect(self.on_page_first)
        self.duplicate_group_list.last_page.pressed.connect(self.on_page_last)
        self.duplicate_group_list.pageIndicatorClicked.connect(self.on_page_jump_request)
        outer_splitter.addWidget(self.duplicate_group_list)
        outer_splitter.addWidget(inner_splitter)

        # Create a central widget and layout to hold the splitters
        central_widget = QtWidgets.QWidget()
        hbox = QtWidgets.QHBoxLayout(central_widget)
        hbox.addWidget(outer_splitter)
        self.setCentralWidget(central_widget)

    def _on_run_timer_tick(self):
        """Timer tick for runtime label, increment the elapsed seconds and draw..."""
        if not self.processor:
            return

        # Only count time while actively running
        if not self.processor.is_paused() and not self.processor.is_finished():
            self._elapsed_secs += 1
            h, rem = divmod(self._elapsed_secs, 3600)
            m, s = divmod(rem, 60)
            self._timer_label.setText(f"{h:02d}:{m:02d}:{s:02d}")

    def _on_labels_tick(self):
        """Timer tick for general labels, runs quickly"""
        if not self.processor:
            return

        self.update_labels()

    def on_precision_adjust(self, e):
        """The precision slider has been adjusted, so update the hash checkbox"""
        if e != 10:
            self.hash_match_chkbx.setEnabled(False)
            self.hash_match_chkbx.setChecked(False)
        else:
            self.hash_match_chkbx.setEnabled(True)

    def on_pause(self, e):
        """Pause button has been pressed, so pause"""
        if not e:
            return

        if not self.processor:
            return

        self.processor.pause()
        self._run_timer.stop()
        self._label_timer.stop()

    def on_start(self, e):
        """Start button has been pressed, so start or resume"""
        if not e:
            return

        if not self.processor:
            # This is the first time running!
            self.processor = ImageMatcher(
                strength=self.precision_slider.value(),
                exact_match=self.hash_match_chkbx.isChecked(),
            )

        elif self.processor.is_paused() and not self.processor.is_finished():
            # Already started so we just need to resume
            self.processor.resume()
            self.pause_btn.setEnabled(True)
            self._run_timer.start()
            self._label_timer.start()
            return

        elif self.processor.running():
            raise RuntimeError("Somehow we're trying to run when the processor appears to be running already!")

        target_paths = self.file_paths_selected()

        self._thread = ProcessorThread(self.processor, target_paths)
        self._thread.signals.new_group.connect(self.on_new_match_group_found)
        self._thread.signals.new_match.connect(self.on_new_match_found)
        self._thread.signals.finish.connect(self.on_finish)
        self._threadpool.start(self._thread)
        self.pause_btn.setEnabled(True)
        self.precision_slider.setEnabled(False)
        self.hash_match_chkbx.setEnabled(False)
        self._run_timer.start()
        self._label_timer.start()
        return

    def on_delete(self, *_):
        """Delete button pressed, process delete file states"""
        self.process_file_states({SelectionState.DELETE})

    def on_ignore(self, *_):
        """Ignore button pressed, process ignore file states"""
        self.process_file_states({SelectionState.IGNORE})

    def on_move(self, *_):
        """Ignore button pressed, process ignore file states"""
        self.process_file_states({SelectionState.MOVE})

    @property
    def total_pages(self):
        """How many pages are there in the duplicate group list"""
        return ceildiv(len(self.processor.matches), self.duplicate_group_list._max_rows) or 1

    def on_finish(self):
        """Finish callback, to update GUI when processing has completed"""
        self.stop_btn.setChecked(True)
        self.pause_btn.setEnabled(False)
        self._run_timer.stop()
        self._label_timer.stop()
        self._thread = None
        self.update_labels()

    def on_new_match_group_found(self, match_group: ImageMatch):
        """New match group found callback, update the GUI with new match group"""
        page_this_belongs_on, _ = divmod(match_group.match_i, self.duplicate_group_list._max_rows)
        self.duplicate_group_list.update_page_indicator(self.current_page, self.total_pages)

        if self.current_page == page_this_belongs_on + 1:
            self.duplicate_group_list.add_group(match_group.matches)

        self.set_duplicate_groups_label(len(self.processor.matches))

    def on_new_match_found(self, response):
        """New match found callback, update the GUI with new match"""
        # First we must decompose the response
        #   TODO: Can I just create a callback with 2 args?
        match_group: ImageMatch
        new_match: ZipPath
        match_group, new_match = response

        page_this_belongs_on, row_this_is = divmod(match_group.match_i, self.duplicate_group_list._max_rows)

        if self.current_page == page_this_belongs_on + 1:
            self.duplicate_group_list._rows[row_this_is].add_tile(new_match)

        self.set_duplicate_images_label(self.processor.duplicate_images)

    def set_duplicate_groups_label(self, duplicate_groups: int):
        """Set the duplicate groups count label"""
        self._dup_groups_label.setText(f"Duplicate groups....{duplicate_groups}")

    def set_duplicate_images_label(self, duplicate_images: int):
        """Set the duplicate images count label"""
        self._dup_pictures_label.setText(f"Duplicate pictures..{duplicate_images}")

    def set_remaining_files_label(self, remaining_files: int):
        """Set the remaining files count label"""
        self._remaining_files_label.setText(f"Remaining files....{remaining_files}")

    def set_loaded_pictures_label(self, loaded_pictures: int):
        """Set the loaded pictures count label"""
        self._loaded_pictures_label.setText(f"Loaded pictures..{loaded_pictures}")

    def update_labels(self):
        """Update all of the boring labels that need to be regularly updated, and the progress bar"""
        if not self.processor:
            return

        if self.processor.found_images:
            self._progress_bar.setMaximum(self.processor.found_images)
            self._progress_bar.setValue(self.processor.processed_images)
        else:
            self._progress_bar.setMaximum(100)
            self._progress_bar.setValue(0)

        self.set_remaining_files_label(self.processor.left_to_process)
        self.set_loaded_pictures_label(self.processor.processed_images)
        self.set_duplicate_groups_label(len(self.processor.matches))
        self.set_duplicate_images_label(self.processor.duplicate_images)

    def on_page_jump_request(self):
        """
        Prompt for a page number and jump there.
        Uses a numeric-only dialog with range [1, total_pages].
        """
        total = self.total_pages
        if total == 1:
            return

        val, ok = QtWidgets.QInputDialog.getInt(
            self,
            "Go to page",
            f"Enter a page number (1-{total}):",
            value=self.current_page,
            minValue=1,
            maxValue=total,
        )
        if not ok:
            return

        if val != self.current_page:
            self.current_page = val
            self.update_group_list()

    def on_page_down(self, *_):
        """The page down button has been pressed"""
        if self.total_pages == 1:
            # Theres only one page so do nothing
            return

        # Make the page system rotate around the beginning
        if self.current_page == 1:
            self.current_page = self.total_pages
        else:
            self.current_page -= 1

        self.update_group_list()

    def on_page_up(self, *_):
        """The page up button has been pressed"""
        if self.total_pages == 1:
            # Theres only one page....
            return

        # Make the page system rotate around the end
        if self.current_page == self.total_pages:
            self.current_page = 1
        else:
            self.current_page += 1

        self.update_group_list()

    def on_page_first(self):
        """Go to first page button pressed"""
        if self.current_page != 1:
            self.current_page = 1
            self.update_group_list()

    def on_page_last(self):
        """Go to last page button pressed"""
        last_page = self.total_pages
        if self.current_page != last_page:
            self.current_page = last_page
            self.update_group_list()

    # region "mark" methods
    #   These mark the currently open file in the view area to something, typically used by the status bar items
    def mark_unmark(self, target_path: ZipPath | None = None):
        """Unmark a file (mark as keep)"""
        self.change_file_state(target_path or self.image_view_area.current_path, SelectionValues.KEEP)

    def mark_delete(self, target_path: ZipPath | None = None):
        """Mark a file as delete"""
        self.change_file_state(target_path or self.image_view_area.current_path, SelectionValues.DELETE)

    def mark_ignore(self, target_path: ZipPath | None = None):
        """Mark a file as ignore"""
        self.change_file_state(target_path or self.image_view_area.current_path, SelectionValues.IGNORE)

    def mark_move(self, target_path: ZipPath | None = None):
        """Prompt for a destination and mark a file to be moved."""

        path = target_path or self.image_view_area.current_path

        if not path or path.is_zip:
            return

        current_file = path.path_obj
        if not current_file.exists():
            return

        destination, _ = QtWidgets.QFileDialog.getSaveFileName(
            self,
            "Move file",
            str(current_file),
        )

        if not destination:
            return

        destination_path = Path(destination)
        if destination_path == current_file:
            logger.info("Marking file as keep because it is the same file...")
            self.change_file_state(path, SelectionValues.KEEP)
            return

        self.change_file_state(path, SelectionValue(SelectionState.MOVE, (destination_path, )))

    def mark_group(self, path, selection: SelectionValue):
        """Mark all files in a group with path as a particular state"""
        if not path:
            return

        currently_paused = self.processor.conditional_pause()

        hash_ = self.processor._reverse_hashes[path]
        for path in self.processor._hashes[hash_].matches:
            if path.path_obj.suffix.lower() == '.zip' and selection == SelectionValues.DELETE:
                continue

            self.file_states[path] = selection

        self.update_selection_states()
        self.processor.conditional_resume(currently_paused)

    def mark_ignore_group(self, target_path: ZipPath | None = None):
        """Mark all files in a group as ignore"""
        self.mark_group(target_path or self.image_view_area.current_path, SelectionValues.IGNORE)

    def mark_delete_group(self, target_path: ZipPath | None = None):
        """Mark all files in a group as delete"""
        self.mark_group(target_path or self.image_view_area.current_path, SelectionValues.DELETE)

    def mark_column(self, column_i: int, selection: SelectionValues):
        """Mark all tiles in a column as a particular state"""
        currently_paused = self.processor.conditional_pause()

        for row in self.duplicate_group_list._rows:
            if column_i >= len(row):
                continue

            target_path = row._tiles[column_i].path
            self.file_states[target_path] = selection

        self.update_selection_states()
        self.processor.conditional_resume(currently_paused)

    # TODO: Make target_column option and get the column of the image view area.
    #    This would allow me to add these options to the menu bar but honestly...
    #    I'm fine with them just being in the context menu...
    def mark_ignore_column(self, target_column: int):
        """Mark all files in a column as ignore"""
        self.mark_column(target_column, SelectionValues.IGNORE)

    def mark_delete_column(self, target_column: int):
        """Mark all files in a column as delete"""
        self.mark_column(target_column, SelectionValues.DELETE)

    def mark_ignore_zip(self, target_path: ZipPath | None = None):
        """Mark all files in a zip as ignore"""
        currently_paused = self.processor.conditional_pause()
        current_zip_path = target_path or self.image_view_area.current_path

        if not current_zip_path:
            return

        if current_zip_path.path_obj.suffix.lower() != '.zip':
            return

        for path in self.processor._processed_zips[current_zip_path.path]:
            self.file_states[path] = SelectionValues.IGNORE

        self.update_selection_states()
        self.processor.conditional_resume(currently_paused)
    # endregion

    def update_group_list(self):
        """Update the duplicate group list"""
        self.image_view_area.clear()

        row_count = self.duplicate_group_list._max_rows
        self.duplicate_group_list.set_groups(
            [m.matches
             for m in self.processor.matches[(self.current_page - 1) * row_count:self.current_page * row_count]],
        )

        self.update_selection_states()
        self.duplicate_group_list.update_page_indicator(self.current_page, self.total_pages)

    def change_file_state(self, path: ZipPath, state: SelectionValue):
        """A tile has been clicked and the match state was changed"""
        if path.path_obj.suffix.lower() == '.zip' and state == SelectionValues.DELETE:
            return

        current_file_state = self.file_states.get(path, SelectionValues.KEEP)
        if current_file_state not in STATE_ORDER:
            if _prompt_move_clear(self, current_file_state.state):
                self.state = state
            return

        self.file_states[path] = state
        self.update_selection_states()

    def on_tile_hover(self, path: ZipPath):
        """A tile has been hovered over, so handle that logic"""
        self.image_view_area.set_image(path)
        self.mark_delete_menu.setEnabled(not path.is_zip)
        self.mark_delete_group_menu.setEnabled(not path.is_zip)
        self.mark_move_menu.setEnabled(not path.is_zip)
        self.mark_ignore_zip_menu.setEnabled(path.is_zip)

    def on_match_state_changed(self, path: ZipPath, state):
        """A tile has been clicked and the match state was changed"""
        self.file_states[path] = state

    def update_selection_states(self, selections: dict | None = None):
        """Update the states of the selection in the duplicate list view"""
        selections = selections or self.file_states

        for group in self.duplicate_group_list._rows:
            for tile in group.tiles():
                set_state = selections.get(tile.path)
                if set_state:
                    tile.silent_set_state(set_state)

    # region File Path Selection display
    def build_file_path_selection_display(self):
        """
        Build the file path selection display,
            which shows the selected file paths and the controls to add/remove them and re-order them

        Returns:
            QtWidgets.QHBoxLayout: The file path selection display with associated controls in a layout
        """
        # TODO: I need better icons here but I can't find the "in"/"out" icons in VP execution data...

        # region Selected File Path sort controls
        file_path_up_control = QtWidgets.QPushButton("^+")
        file_path_up_control.setSizePolicy(MAX_SIZE_POLICY)
        file_path_up_control.clicked.connect(self.file_path_up_clicked)
        file_path_down_control = QtWidgets.QPushButton("V-")
        file_path_down_control.setSizePolicy(MAX_SIZE_POLICY)
        file_path_down_control.clicked.connect(self.file_path_down_clicked)

        file_path_sort_controls = QtWidgets.QVBoxLayout()
        file_path_sort_controls.setContentsMargins(NO_MARGIN)
        file_path_sort_controls.addWidget(file_path_up_control)
        file_path_sort_controls.addWidget(file_path_down_control)
        # endregion

        # region Selected File Path selection controls
        file_path_in_control = QtWidgets.QPushButton(">+")
        file_path_in_control.setSizePolicy(MAX_SIZE_POLICY)
        file_path_in_control.clicked.connect(self.file_path_in_clicked)
        file_path_out_control = QtWidgets.QPushButton("<-")
        file_path_out_control.setSizePolicy(MAX_SIZE_POLICY)
        file_path_out_control.clicked.connect(self.file_path_out_clicked)

        file_path_io_controls = QtWidgets.QVBoxLayout()
        file_path_io_controls.setContentsMargins(NO_MARGIN)
        file_path_io_controls.addWidget(file_path_in_control)
        file_path_io_controls.addWidget(file_path_out_control)
        # endregion

        self.selected_file_path_display = QtWidgets.QListWidget()

        file_path_controls = QtWidgets.QHBoxLayout()
        file_path_controls.setContentsMargins(NO_MARGIN)
        file_path_controls.addWidget(QtWidgets.QWidget(layout=file_path_io_controls, maximumWidth=50))
        file_path_controls.addWidget(self.selected_file_path_display)
        file_path_controls.addWidget(QtWidgets.QWidget(layout=file_path_sort_controls, maximumWidth=50))
        return file_path_controls

    def file_path_up_clicked(self, _):
        """Move the selected directory up in the ordering"""
        # TODO: If the path has already been loaded for processing in the processor, then this won't do much
        for selected_index in self.selected_file_path_display.selectedIndexes():
            row = selected_index.row()
            if row == 0:
                continue

            item = self.selected_file_path_display.takeItem(row)
            self.selected_file_path_display.insertItem(row - 1, item)
            self.selected_file_path_display.setCurrentIndex(self.selected_file_path_display.indexFromItem(item))

    def file_path_down_clicked(self, _):
        """Move the selected directory up in the ordering"""
        # TODO: If the path has already been loaded for processing in the processor, then this won't do much
        for selected_index in self.selected_file_path_display.selectedIndexes():
            row = selected_index.row()
            if row == self.selected_file_path_display.count():
                continue

            item = self.selected_file_path_display.takeItem(row)
            self.selected_file_path_display.insertItem(row + 1, item)
            self.selected_file_path_display.setCurrentIndex(self.selected_file_path_display.indexFromItem(item))

    def file_path_in_clicked(self, _):
        """Add the selected directory, and add to processing if processing has been started"""
        selected_indexes = self.file_system_view.selectedIndexes()
        for index in selected_indexes:
            info = self.file_system_view.model().fileInfo(index)
            target_path = info.filePath()
            self.selected_file_path_display.addItem(target_path)

            if self.processor and self.processor.running():
                self.processor.add_path(target_path)

    def file_path_out_clicked(self, _):
        """Move the selected directory out of processing"""
        # TODO: If the path has already been loaded for processing in the processor then all of the images
        #   will still need to be hashed before the callback will discard the results...
        for selected_index in self.selected_file_path_display.selectedIndexes():
            selected_item = self.selected_file_path_display.takeItem(selected_index.row())

            if self.processor:
                self.processor.remove_path(selected_item.text())
                self.update_group_list()
                self.update_labels()

    def file_paths_selected(self):
        """Get the file paths selected in the selected file path display"""
        return [
            self.selected_file_path_display.item(i).text()
            for i in range(self.selected_file_path_display.count())
        ]
    # endregion

    # region Image View area (bottom-right)
    def build_image_view_area(self):
        """Build the image view area. Fairly simple now that the widget does most of the work"""
        self.preview_resized.changed.connect(self.preview_resized_changed)
        self.image_view_area = ImageViewPane()
        return self.image_view_area

    def preview_resized_changed(self):
        """The preview sizing option has changed"""
        self.image_view_area.set_index(int(not self.preview_resized.isChecked()))
    # endregion

    def build_statusbar(self) -> None:
        """Creates a simple status bar for hints and counts."""
        sb = QtWidgets.QStatusBar()
        self.setStatusBar(sb)
        self.count_label = QtWidgets.QLabel("No groups loaded")
        sb.addPermanentWidget(self.count_label)

    def process_file_states(self, states: set[SelectionState] | None = None):
        """Process the set file states"""
        self.image_view_area.clear()

        if not states:
            states = {SelectionState.DELETE, SelectionState.IGNORE, SelectionState.MOVE}

        states.add(SelectionState.KEEP)

        is_paused = self.processor.conditional_pause()

        file_size_deleted = 0
        file_count_deleted = 0
        file_count_ignored = 0
        file_count_moved = 0
        failed_file_deletes = []

        for file, set_state in self.file_states.items():
            if set_state.state not in states:
                continue

            if set_state.state == SelectionState.DELETE:
                logger.info("Deleting %s", file)
                path = file.path_obj
                try:
                    file_size_deleted += path.stat().st_size
                    path.unlink()
                except PermissionError:
                    logger.info("Failed to delete %s, it is in use!", file)
                    failed_file_deletes.append(file)
                    continue
                except FileNotFoundError:
                    logger.info("File already deleted...", file)

                self.processor.remove(file)
                file_count_deleted += 1
            elif set_state.state == SelectionState.IGNORE:
                self.processor.ignore(file)
                file_count_ignored += 1
            elif set_state.state == SelectionState.KEEP:
                pass
            elif set_state.state == SelectionState.MOVE:
                destination = Path(set_state.args[0])
                if destination.is_dir():
                    destination /= file.path_obj.name

                if destination.is_file():
                    destination.unlink()

                destination.parent.mkdir(parents=True, exist_ok=True)
                logger.info("Moving %s to %s", file.path_obj, destination)
                shutil.move(str(file.path_obj), str(destination))

                if any(_is_under(f, destination) for f in self.file_paths_selected()):
                    dest = ZipPath(str(destination))
                    hash_ = self.processor._reverse_hashes.pop(file)
                    self.processor._reverse_hashes[dest] = hash_

                    self.processor._hashes[hash_].matches.remove(file)
                    self.processor._hashes[hash_].matches.append(dest)
                else:
                    # This file is no longer in a selected folder! So remove it from the matching
                    self.processor.remove(file)
                    file_count_ignored += 1
                file_count_moved += 1
            else:
                raise NotImplementedError(f"Unknown state {set_state.state.value}")

        # Clear the states which we have processed
        self.file_states = {
            k: v
            for k, v in self.file_states.items()
            if v.state not in states or k in failed_file_deletes
        }

        # If total pages has decreased passed the current page, then make sure to set the current page
        if self.current_page > self.total_pages:
            self.current_page = self.total_pages

        # Update the GUI:
        self.update_labels()
        self.update_group_list()

        # Resume processing
        self.processor.conditional_resume(is_paused)

        # region Final status popup
        popup_text = ""

        if file_count_deleted:
            popup_text += f"Deleted {file_count_deleted} files for a savings of {human_bytes(file_size_deleted)}.\n"

        if file_count_ignored:
            popup_text += f"Removed {file_count_ignored} files from matching.\n"

        if file_count_moved:
            popup_text += f"Moved {file_count_moved} file.\n"

        if popup_text:
            dlg = QtWidgets.QMessageBox(self)
            dlg.setWindowTitle("Result")
            dlg.setText(popup_text)
            dlg.exec()
        # endregion

    def closeEvent(self, event: QtGui.QCloseEvent) -> None:
        """Stop the processor if closing, and confirm close if there is data loaded"""
        if self.processor:
            if len(self.processor.matches) and not self.confirm_close():
                # There IS data loaded and user said NO to exiting
                event.ignore()
                return

            if self.processor.running():
                self.processor.finish()

        event.accept()

    def confirm_close(self) -> bool:
        """
        Show a confirmation dialog asking if the user really wants to close.

        Returns:
            True if the user confirmed closing; False to keep the app open.
        """
        return self.exit_warning.exec() == QtWidgets.QMessageBox.StandardButton.Yes
