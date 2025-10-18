# TODO: Confirm to close
# TODO: Validate that users don't select overlapping paths...

from pathlib import Path

from PySide6 import QtCore, QtGui, QtWidgets

from pixmatch import ImageMatcher, ImageMatch, NewGroup, NewMatch, Finished
from pixmatch.gui.utils import NO_MARGIN, MAX_SIZE_POLICY
from pixmatch.gui.widgets import DuplicateGroupList, DirFileSystemModel, ImageViewPane, SelectionState



class WorkerSignals(QtCore.QObject):
    """Signals from a running worker thread.

    finished
        No data

    error
        tuple (exctype, value, traceback.format_exc())

    result
        object data returned from processing, anything

    progress
        float indicating % progress
    """

    new_match = QtCore.Signal(tuple)
    new_group = QtCore.Signal(object)
    finish = QtCore.Signal()


class ProcessorThread(QtCore.QRunnable):

    def __init__(self, processor, *args, **kwargs):
        super().__init__()
        self.args = args
        self.kwargs = kwargs
        self.processor = processor
        self.signals = WorkerSignals()

        # timer lives on the GUI thread; it polls the library queue
        self._poller = QtCore.QTimer()
        self._poller.setInterval(20)
        self._poller.timeout.connect(self._drain_events)
        self._poller.start()

    def _drain_events(self):
        while not self.processor.events.empty():
            evt = self.processor.events.get_nowait()
            if isinstance(evt, NewGroup):
                self.signals.new_group.emit(evt.group)
            elif isinstance(evt, NewMatch):
                self.signals.new_match.emit((evt.group, evt.path))
            elif isinstance(evt, Finished):
                self.signals.finish.emit()

    def run(self):
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
        self.setWindowTitle("PixMatch — MVP GUI")
        self.resize(1200, 800)

        # State
        self.current_page: int = 0
        self.processor = None
        self.file_states = dict()
        self.threadpool = QtCore.QThreadPool()
        self._gui_paused = False

        # UI skeleton
        self.build_menubar()
        self.build_central()
        self.build_statusbar()

        for start_path in start_paths or []:
            self.selected_file_path_display.addItem(str(start_path))

    # ----- UI builders -----------------------------------------------------

    def build_menubar(self) -> None:
        """Creates the top menu bar"""
        menu = self.menuBar()

        # region File menu
        load_project = QtGui.QAction("Load Project...", self)
        save_project = QtGui.QAction("Save Project...", self)
        exit_project = QtGui.QAction("Exit", self)

        file_menu = menu.addMenu("&File")
        file_menu.addAction(load_project)
        file_menu.addAction(save_project)
        file_menu.addSeparator()
        file_menu.addAction(exit_project)
        # endregion

        # region Edit menu
        mark_delete = QtGui.QAction("Delete", self)
        mark_ignore = QtGui.QAction("Ignore", self)
        mark_ignore_group = QtGui.QAction("Ignore Group", self)
        mark_ignore_folder = QtGui.QAction("Ignore Folder", self)
        mark_rename = QtGui.QAction("Rename this file...", self)
        mark_move = QtGui.QAction("Move this file...", self)
        unmark = QtGui.QAction("Un-select", self)

        edit_menu = menu.addMenu("&Edit")
        edit_menu.addAction(mark_delete)
        edit_menu.addAction(mark_ignore)
        edit_menu.addAction(mark_ignore_group)
        edit_menu.addAction(mark_ignore_folder)
        edit_menu.addSeparator()
        edit_menu.addAction(mark_rename)
        edit_menu.addAction(mark_move)
        edit_menu.addSeparator()
        edit_menu.addAction(unmark)
        # endregion

        # region View menu
        page_next = QtGui.QAction("Next page", self)
        page_back = QtGui.QAction("Previous page", self)
        self.preview_resized = QtGui.QAction("Preview resized", self, checked=True, checkable=True)
        preview_full_size = QtGui.QAction("Preview full size", self, checkable=True)

        preview_options_grp = QtGui.QActionGroup(self)
        preview_options_grp.setExclusive(True)
        preview_options_grp.addAction(preview_full_size)
        preview_options_grp.addAction(self.preview_resized)

        # TODO: I'm not sure why we would want to slow display?
        #   and show differences has never worked for me
        # mark_rename = QtGui.QAction("Slow preview display", self)
        # mark_move = QtGui.QAction("Show differences", self)

        view_menu = menu.addMenu("&View")
        view_menu.addAction(page_next)
        view_menu.addAction(page_back)
        view_menu.addSeparator()
        view_menu.addAction(self.preview_resized)
        view_menu.addAction(preview_full_size)
        # endregion

        # region Tools menu
        autoselect = QtGui.QAction("Auto-select", self)

        tool_menu = menu.addMenu("&Tools")
        tool_menu.addAction(autoselect)
        # endregion

        # region Actions menu
        run_move = QtGui.QAction("Move", self)
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
        option_hidden_folders = QtGui.QAction("Show hidden folders", self, checkable=True)
        option_subfolders = QtGui.QAction("Include subfolders", self, checkable=True)
        option_rotations = QtGui.QAction("Scan for rotations", self, checkable=True)

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
        # TODO: Add other setting tabs
        style = QtWidgets.QApplication.instance().style()

        # region General controls area (top-right)
        # region Control buttons
        autoselect_btn = QtWidgets.QPushButton("Auto-select")

        tools = QtWidgets.QVBoxLayout()
        tools.addWidget(autoselect_btn)

        tool_box = QtWidgets.QGroupBox("Tools")
        tool_box.setLayout(tools)
        tool_box.setMaximumHeight(60)

        move_btn = QtWidgets.QPushButton("Move")
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
        self._remaining_files_label = QtWidgets.QLabel("Remaining files....0")
        self._loaded_pictures_label = QtWidgets.QLabel("Loaded pictures..0")
        self._to_compare_label = QtWidgets.QLabel("To compare.......0")
        self._dup_pictures_label = QtWidgets.QLabel("Duplicate pictures..0")
        self.set_duplicate_images_label(0)
        self._dup_groups_label = QtWidgets.QLabel("Duplicate groups..0")
        self.set_duplicate_groups_label(0)

        self._timer_label = QtWidgets.QLabel("00:00:00", alignment=QtCore.Qt.AlignmentFlag.AlignHCenter)
        self._elapsed_secs = 0
        self._timer = QtCore.QTimer(self)
        self._timer.setInterval(1000)  # 1s ticks
        self._timer.timeout.connect(self._on_timer_tick)

        self._progress_bar = QtWidgets.QProgressBar(value=50, textVisible=False)
        labels.addWidget(self._remaining_files_label)
        labels.addWidget(self._loaded_pictures_label)
        labels.addWidget(self._to_compare_label)
        labels.addWidget(self._dup_pictures_label)
        labels.addWidget(self._dup_groups_label)
        labels.addWidget(self._timer_label)
        labels.addWidget(self._progress_bar)
        labels.addLayout(run_controls)

        # region Settings tabs
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
        primary_controls.addWidget(QtWidgets.QWidget(layout=labels_and_such, fixedHeight=240))

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
        self.duplicate_group_list.groupTileHovered.connect(self.image_view_area.set_image)
        self.duplicate_group_list.left_arrow.pressed.connect(self.on_page_down)
        self.duplicate_group_list.right_arrow.pressed.connect(self.on_page_up)
        outer_splitter.addWidget(self.duplicate_group_list)
        outer_splitter.addWidget(inner_splitter)

        # Create a central widget and layout to hold the splitters
        central_widget = QtWidgets.QWidget()
        hbox = QtWidgets.QHBoxLayout(central_widget)
        hbox.addWidget(outer_splitter)
        self.setCentralWidget(central_widget)

    def _on_timer_tick(self):
        if not self.processor:
            return

        # Only count time while actively running
        if not self.processor.is_paused() and not self.processor.is_finished():
            self._elapsed_secs += 1
            h, rem = divmod(self._elapsed_secs, 3600)
            m, s = divmod(rem, 60)
            self._timer_label.setText(f"{h:02d}:{m:02d}:{s:02d}")

    def on_precision_adjust(self, e):
        """The precision slider has been adjusted, so update the hash checkbox"""
        if e != 10:
            self.hash_match_chkbx.setEnabled(False)
            self.hash_match_chkbx.setChecked(False)
        else:
            self.hash_match_chkbx.setEnabled(True)

    def on_pause(self, e):
        if not e:
            return

        if not self.processor:
            return

        self.processor.pause()
        self._timer.stop()

    def on_start(self, e):
        if not e:
            return

        if not self.processor:
            # self.duplicate_group_list._clear_rows()
            self.processor = ImageMatcher(
                strength=self.precision_slider.value(),
                exact_match=self.hash_match_chkbx.isChecked(),
            )

            target_paths = [
                self.selected_file_path_display.item(i).text()
                for i in range(self.selected_file_path_display.count())
            ]

            thread = ProcessorThread(self.processor, target_paths)
            thread.signals.new_group.connect(self.on_new_match_group_found)
            thread.signals.new_match.connect(self.on_new_match_found)
            thread.signals.finish.connect(self.on_finish)
            self.threadpool.start(thread)
            self.pause_btn.setEnabled(True)
            self._elapsed_secs = 0
            self._timer.start()
            # TODO: Disable strength slider, re-enable only once finished or stopped
            return

        if self.processor.is_paused() and not self.processor.is_finished():
            self.processor.resume()
            self.pause_btn.setEnabled(True)
            self._timer.start()
            return

        # TODO: Should probably pop up a warning box? Need to clear the processor, all labels and the matches list
        raise NotImplementedError("Have not implemented doing a second run after a completed first run")

    def on_finish(self):
        self.pause_btn.setEnabled(True)
        self.stop_btn.setChecked(False)
        self._timer.stop()

    def on_delete(self, *_):
        self.process_file_states({SelectionState.DELETE})

    def on_ignore(self, *_):
        self.process_file_states({SelectionState.IGNORE})

    @property
    def last_page(self):
        return len(self.processor.matches) // self.duplicate_group_list._max_rows

    def on_new_match_group_found(self, match_group: ImageMatch):
        if self._gui_paused:
            return

        self.duplicate_group_list.update_page_indicator(self.current_page + 1, self.last_page + 1)

        if self.current_page == self.last_page:
            self.duplicate_group_list.add_group(match_group.matches)

        self.set_duplicate_groups_label(len(self.processor.matches))

    def on_new_match_found(self, response):
        match_group: ImageMatch
        new_match: Path
        match_group, new_match = response

        if self._gui_paused:
            return

        page_this_belongs_on, row_this_is = divmod(match_group.match_i, self.duplicate_group_list._max_rows)

        if self.current_page == page_this_belongs_on:
            self.duplicate_group_list._rows[row_this_is].add_tile(new_match)

        self.set_duplicate_images_label(self.processor.duplicate_images)

    def set_duplicate_groups_label(self, duplicate_groups):
        self._dup_groups_label.setText(f"Remaining files....{duplicate_groups}")

    def set_duplicate_images_label(self, duplicate_images):
        self._dup_pictures_label.setText(f"Loaded pictures..{duplicate_images}")

    def update_labels(self):
        self.set_duplicate_groups_label(len(self.processor.matches))
        self.set_duplicate_images_label(self.processor.duplicate_images)

    def on_page_down(self):
        if self.current_page == 0:
            self.current_page = self.last_page
        else:
            self.current_page -= 1

        self.update_group_list()

    def on_page_up(self):
        if self.current_page == self.last_page:
            self.current_page = 0
        else:
            self.current_page += 1

        self.update_group_list()

    def update_group_list(self):
        row_count = self.duplicate_group_list._max_rows
        self.duplicate_group_list.set_groups(
            [m.matches
             for m in self.processor.matches[self.current_page * row_count:(self.current_page + 1) * row_count]]
        )

        for group in self.duplicate_group_list._rows:
            for tile in group.tiles():
                set_state = self.file_states.get(Path(tile.path))
                if set_state:
                    tile.state = set_state

    def on_match_state_changed(self, path, state):
        self.file_states[Path(path)] = state

        for group in self.duplicate_group_list._rows:
            for tile in group.tiles():
                if tile.path == path:
                    tile.state = state

    # region File Path Selection display
    def build_file_path_selection_display(self):

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
        for selected_index in self.selected_file_path_display.selectedIndexes():
            row = selected_index.row()
            if row == 0:
                continue

            item = self.selected_file_path_display.takeItem(row)
            self.selected_file_path_display.insertItem(row - 1, item)
            self.selected_file_path_display.setCurrentIndex(self.selected_file_path_display.indexFromItem(item))

    def file_path_down_clicked(self, _):
        for selected_index in self.selected_file_path_display.selectedIndexes():
            row = selected_index.row()
            if row == self.selected_file_path_display.count():
                continue

            item = self.selected_file_path_display.takeItem(row)
            self.selected_file_path_display.insertItem(row + 1, item)
            self.selected_file_path_display.setCurrentIndex(self.selected_file_path_display.indexFromItem(item))

    def file_path_in_clicked(self, _):
        selected_indexes = self.file_system_view.selectedIndexes()
        for index in selected_indexes:
            info = self.file_system_view.model().fileInfo(index)
            self.selected_file_path_display.addItem(info.filePath())

    def file_path_out_clicked(self, _):
        # TODO: Remove any files which part of this path
        for selected_index in self.selected_file_path_display.selectedIndexes():
            self.selected_file_path_display.takeItem(selected_index.row())
    # endregion

    # region Image View area (bottom-right)
    def build_image_view_area(self):
        self.preview_resized.changed.connect(self.preview_resized_changed)
        self.image_view_area = ImageViewPane()
        return self.image_view_area

    def preview_resized_changed(self):
        self.image_view_area.set_index(int(not self.preview_resized.isChecked()))
    # endregion

    def build_statusbar(self) -> None:
        """Creates a simple status bar for hints and counts."""
        sb = QtWidgets.QStatusBar()
        self.setStatusBar(sb)
        self.count_label = QtWidgets.QLabel("No groups loaded")
        sb.addPermanentWidget(self.count_label)

    def process_file_states(self, states=None):
        if not states:
            states = {SelectionState.DELETE, SelectionState.IGNORE}

        states.add(SelectionState.KEEP)

        is_paused = self.processor.is_paused()
        if not is_paused:
            self.processor.pause()

        for file, set_state in self.file_states.items():
            if set_state not in states:
                continue

            if set_state == SelectionState.DELETE:
                # TODO: This is just for testing:
                print(f"Deleting {file}")
                # file.unlink()
                self.processor.remove(file)
            elif set_state == SelectionState.IGNORE:
                self.processor.remove(file)
            elif set_state == SelectionState.KEEP:
                pass

        self.file_states = {
            k: v
            for k, v in self.file_states.items()
            if v not in states
        }

        self.update_labels()
        self.update_group_list()
        if not is_paused:
            self.processor.resume()
