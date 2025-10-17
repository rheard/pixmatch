import os

from enum import Enum, auto
from typing import Dict, Iterable, List, Sequence

from PIL import Image
from PySide6 import QtCore, QtGui, QtWidgets


NO_MARGIN = QtCore.QMargins(0, 0, 0, 0)

MAX_SIZE_POLICY = QtWidgets.QSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding,
                                        QtWidgets.QSizePolicy.Policy.Expanding)
