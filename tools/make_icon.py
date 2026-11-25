# -*- coding: utf-8 -*-
"""生成应用图标：assets/app.ico（多尺寸）。

    python tools/make_icon.py
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

SIZES = [16, 24, 32, 48, 64, 128, 256]


def draw(size: int):
    from PySide6.QtCore import QPointF, QRectF, Qt
    from PySide6.QtGui import (QBrush, QColor, QFont, QLinearGradient, QPainter,
                               QPainterPath, QPen, QPixmap)

    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing, True)
    p.setRenderHint(QPainter.TextAntialiasing, True)

    # 圆角底板（蓝紫渐变）
    r = size * 0.22
    grad = QLinearGradient(0, 0, size, size)
    grad.setColorAt(0.0, QColor("#4f7cff"))
    grad.setColorAt(1.0, QColor("#7b5cff"))
    path = QPainterPath()
    path.addRoundedRect(QRectF(0, 0, size, size), r, r)
    p.fillPath(path, QBrush(grad))

    # 放大镜
    cx, cy = size * 0.44, size * 0.42
    rad = size * 0.20
    pen = QPen(QColor("#ffffff"))
    pen.setWidthF(max(1.6, size * 0.075))
    pen.setCapStyle(Qt.RoundCap)
    p.setPen(pen)
    p.setBrush(Qt.NoBrush)
    p.drawEllipse(QPointF(cx, cy), rad, rad)
    # 手柄
    p.drawLine(QPointF(cx + rad * 0.72, cy + rad * 0.72),
               QPointF(size * 0.76, size * 0.76))
    p.end()
    return pm


def main() -> int:
    from PySide6.QtGui import QIcon
    from PySide6.QtWidgets import QApplication

    app = QApplication(sys.argv)      # noqa: F841  QPixmap 需要 QApplication
    outdir = os.path.join(HERE, "assets")
    os.makedirs(outdir, exist_ok=True)

    icon = QIcon()
    for s in SIZES:
        icon.addPixmap(draw(s))
    ico = os.path.join(outdir, "app.ico")
    # QIcon 不能直接写 ico，用 PNG 拼装
    png = os.path.join(outdir, "app.png")
    draw(256).save(png, "PNG")

    try:
        from PIL import Image

        Image.open(png).save(ico, sizes=[(s, s) for s in SIZES])
    except Exception as exc:  # noqa: BLE001
        print("Pillow 不可用(%s)，改用 QPixmap 逐尺寸写入" % exc)
        # 手工拼 ICO：没有 Pillow 时至少给一个 256 PNG 版本
        draw(256).save(ico.replace(".ico", "-256.png"), "PNG")
        raise SystemExit("请安装 pillow 后重试：pip install pillow")

    print("icon ->", ico)
    return 0


if __name__ == "__main__":
    sys.exit(main())
