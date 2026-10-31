"""Deterministic rendering for the desktop pet.

Every frame is produced from the authoritative character image plus vector
effects.  No generated frames are checked in; the master PNG is the source of
truth and is never modified.
"""
from __future__ import annotations

import math
import random
import re
import textwrap
from pathlib import Path
from typing import Iterable

from PIL import Image, ImageDraw, ImageFont

from .states import (
    ALL_STATES, APPROVAL, CODING, ERROR, EXECUTING, IDLE, LISTENING,
    RESEARCHING, SPEAKING, STATE_META, SUCCESS, THINKING, WORKING,
)

try:  # optional but installed for correct Arabic bubble rendering
    import arabic_reshaper
    from bidi.algorithm import get_display
except Exception:  # pragma: no cover - optional
    arabic_reshaper = None
    get_display = None

_ARABIC = re.compile(r"[\u0600-\u06FF]")
_FONT_REGULAR = Path("C:/Windows/Fonts/segoeui.ttf")
_FONT_BOLD = Path("C:/Windows/Fonts/segoeuib.ttf")


def _hex_rgb(value: str) -> tuple[int, int, int]:
    value = str(value or "#6ec6ff").lstrip("#")
    if len(value) != 6:
        value = "6ec6ff"
    return tuple(int(value[i:i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]


def _shape_text(text: str) -> str:
    if not text or not _ARABIC.search(text) or arabic_reshaper is None or get_display is None:
        return text
    try:
        return get_display(arabic_reshaper.reshape(text))
    except Exception:
        return text


def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    path = _FONT_BOLD if bold and _FONT_BOLD.exists() else _FONT_REGULAR
    try:
        return ImageFont.truetype(str(path), size)
    except Exception:
        return ImageFont.load_default()


class PetRenderer:
    def __init__(self, master_path: Path, width: int = 380, height: int = 470,
                 scale: float = 1.0, show_state_label: bool = True):
        self.width = int(width)
        self.height = int(height)
        self.show_state_label = show_state_label
        master = Image.open(master_path).convert("RGBA")
        target_h = max(180, int(300 * float(scale)))
        target_w = max(1, round(master.width * target_h / master.height))
        self.pet = master.resize((target_w, target_h), Image.LANCZOS)
        self.pet_w, self.pet_h = self.pet.size
        self.pet_x = (self.width - self.pet_w) // 2
        self.pet_y = self.height - self.pet_h - 34
        self._rot_cache: dict[int, Image.Image] = {}
        self._fonts: dict[tuple[int, bool], ImageFont.FreeTypeFont] = {}
        self._particles = self._make_particles()

    # ------------------------------------------------------------------ helpers
    def _font(self, size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
        key = (size, bold)
        if key not in self._fonts:
            self._fonts[key] = _font(size, bold)
        return self._fonts[key]

    def _rotated(self, angle: float) -> Image.Image:
        key = int(round(angle))
        key = max(-4, min(4, key))
        if key not in self._rot_cache:
            self._rot_cache[key] = self.pet.rotate(
                key, resample=Image.BICUBIC, expand=False)
        return self._rot_cache[key]

    def _make_particles(self) -> list[dict]:
        rng = random.Random(1307)
        return [{
            "x": rng.uniform(-1, 1),
            "y": rng.uniform(-1, 1),
            "phase": rng.uniform(0, math.tau),
            "size": rng.uniform(1.5, 3.4),
            "speed": rng.uniform(0.5, 1.4),
        } for _ in range(18)]

    def _wrap(self, text: str, font: ImageFont.FreeTypeFont,
              max_width: int, max_lines: int = 6) -> list[str]:
        text = re.sub(r"\s+", " ", str(text or "")).strip()
        if not text:
            return []
        words = text.split(" ")
        lines: list[str] = []
        current = ""
        for word in words:
            candidate = (current + " " + word).strip()
            if font.getlength(candidate) <= max_width or not current:
                current = candidate
            else:
                lines.append(current)
                current = word
                if len(lines) >= max_lines:
                    break
        if current and len(lines) < max_lines:
            lines.append(current)
        if len(lines) >= max_lines:
            used = " ".join(lines).strip()
            if len(used) < len(text):
                lines[-1] = lines[-1].rstrip(" .،") + "…"
        return lines

    def _pill(self, canvas: Image.Image, center: tuple[int, int], text: str,
              fill: tuple[int, int, int, int], fg: tuple[int, int, int, int],
              font_size: int = 12, bold: bool = False) -> None:
        font = self._font(font_size, bold)
        shown = _shape_text(text)
        box = font.getbbox(shown)
        tw = box[2] - box[0]
        th = box[3] - box[1]
        pad_x, pad_y = 10, 5
        x0 = center[0] - tw // 2 - pad_x
        y0 = center[1] - th // 2 - pad_y
        x1 = center[0] + tw // 2 + pad_x
        y1 = center[1] + th // 2 + pad_y
        layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(layer)
        draw.rounded_rectangle((x0, y0, x1, y1), radius=(y1 - y0) // 2,
                               fill=fill, outline=(255, 255, 255, 35), width=1)
        draw.text((center[0], center[1] - th // 2 - box[1]), shown,
                  font=font, fill=fg, anchor="ma")
        canvas.alpha_composite(layer)

    # ------------------------------------------------------------------ effects
    def _draw_glow(self, canvas: Image.Image, color: tuple[int, int, int],
                   t: float, strength: float = 1.0) -> None:
        layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(layer)
        cx = self.pet_x + self.pet_w // 2
        cy = self.pet_y + self.pet_h // 2
        pulse = 0.5 + 0.5 * math.sin(t * 1.8)
        for i in range(3):
            radius = int(self.pet_w * (0.52 + i * 0.05) + pulse * 4)
            alpha = int((10 + i * 7) * strength)
            draw.ellipse((cx - radius, cy - radius, cx + radius, cy + radius),
                         fill=(*color, alpha))
        canvas.alpha_composite(layer)

    def _draw_shadow(self, canvas: Image.Image) -> None:
        layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(layer)
        cx = self.pet_x + self.pet_w // 2
        y = self.pet_y + self.pet_h - 4
        rx = int(self.pet_w * 0.38)
        for i in range(4):
            alpha = 46 - i * 10
            draw.ellipse((cx - rx + i * 8, y - 6 + i * 3,
                          cx + rx - i * 8, y + 8 + i * 3),
                         fill=(0, 0, 0, max(0, alpha)))
        canvas.alpha_composite(layer)

    def _draw_orbit(self, canvas: Image.Image, color: tuple[int, int, int], t: float) -> None:
        layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(layer)
        cx = self.pet_x + self.pet_w // 2
        cy = self.pet_y + self.pet_h // 3
        radius = int(self.pet_w * 0.56)
        for i in range(5):
            angle = t * 1.15 + i * math.tau / 5
            x = cx + math.cos(angle) * radius
            y = cy + math.sin(angle) * radius * 0.52
            alpha = 140 + int(70 * math.sin(t * 3 + i))
            draw.ellipse((x - 3, y - 3, x + 3, y + 3),
                         fill=(*color, max(40, alpha)))
        # magnifier
        angle = t * 1.15
        x = cx + math.cos(angle) * radius
        y = cy + math.sin(angle) * radius * 0.52
        draw.ellipse((x - 8, y - 8, x + 8, y + 8), outline=(*color, 220), width=2)
        draw.line((x + 6, y + 6, x + 13, y + 13), fill=(*color, 220), width=3)
        canvas.alpha_composite(layer)

    def _draw_code(self, canvas: Image.Image, color: tuple[int, int, int], t: float) -> None:
        layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(layer)
        font = self._font(13, True)
        glyphs = ("</>", "{ }", "01", "=>", "()")
        for i, glyph in enumerate(glyphs):
            x = self.pet_x + self.pet_w * (0.12 + 0.19 * i)
            y = self.pet_y + 22 + 18 * math.sin(t * 1.7 + i)
            alpha = 130 + int(90 * math.sin(t * 2.4 + i * 1.7))
            draw.text((x, y), glyph, font=font, fill=(*color, max(50, alpha)))
        canvas.alpha_composite(layer)

    def _draw_sparks(self, canvas: Image.Image, color: tuple[int, int, int], t: float) -> None:
        layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(layer)
        base_x = self.pet_x + self.pet_w // 2
        base_y = self.pet_y + self.pet_h - 6
        for i in range(12):
            phase = (t * 0.9 + i / 12) % 1.0
            y = base_y - phase * (self.pet_h * 0.72)
            x = base_x + math.sin(i * 2.1 + t * 3) * (10 + phase * 34)
            alpha = int(230 * (1 - phase))
            size = 2 + 2 * (1 - phase)
            draw.ellipse((x - size, y - size, x + size, y + size),
                         fill=(*color, max(0, alpha)))
        canvas.alpha_composite(layer)

    def _draw_waves(self, canvas: Image.Image, color: tuple[int, int, int], t: float) -> None:
        layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(layer)
        cx = self.pet_x + int(self.pet_w * 0.78)
        cy = self.pet_y + int(self.pet_h * 0.42)
        for i in range(3):
            radius = int(10 + i * 10 + (math.sin(t * 5) + 1) * 3)
            alpha = max(30, 180 - i * 45)
            draw.arc((cx - radius, cy - radius, cx + radius, cy + radius),
                     start=-55, end=55, fill=(*color, alpha), width=2)
        canvas.alpha_composite(layer)

    def _draw_dots(self, canvas: Image.Image, color: tuple[int, int, int], t: float) -> None:
        layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(layer)
        cx = self.pet_x + self.pet_w // 2
        cy = self.pet_y - 14
        for i in range(3):
            alpha = 80 + int(150 * (0.5 + 0.5 * math.sin(t * 5 - i * 0.8)))
            y = cy - 3 * math.sin(t * 5 - i * 0.8)
            draw.ellipse((cx - 18 + i * 18 - 3, y - 3,
                          cx - 18 + i * 18 + 3, y + 3),
                         fill=(*color, alpha))
        canvas.alpha_composite(layer)

    def _draw_gear(self, canvas: Image.Image, color: tuple[int, int, int], t: float) -> None:
        layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(layer)
        cx = self.pet_x + self.pet_w // 2
        cy = self.pet_y + 10
        radius = 18
        for i in range(8):
            angle = t * 2.0 + i * math.tau / 8
            x = cx + math.cos(angle) * radius
            y = cy + math.sin(angle) * radius
            draw.ellipse((x - 2.5, y - 2.5, x + 2.5, y + 2.5),
                         fill=(*color, 190))
        draw.arc((cx - radius, cy - radius, cx + radius, cy + radius),
                 start=0, end=270, fill=(*color, 220), width=2)
        canvas.alpha_composite(layer)

    def _draw_alert(self, canvas: Image.Image, color: tuple[int, int, int], t: float) -> None:
        layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(layer)
        cx = self.pet_x + self.pet_w // 2
        cy = self.pet_y + self.pet_h // 2
        radius = int(self.pet_w * 0.56 + 6 * math.sin(t * 8))
        draw.ellipse((cx - radius, cy - radius, cx + radius, cy + radius),
                     outline=(*color, 170), width=3)
        font = self._font(28, True)
        draw.text((cx, self.pet_y - 30), "!", font=font, fill=(*color, 240),
                  anchor="mm")
        canvas.alpha_composite(layer)

    def _draw_sparkle(self, canvas: Image.Image, color: tuple[int, int, int], t: float) -> None:
        layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(layer)
        cx = self.pet_x + self.pet_w // 2
        cy = self.pet_y + self.pet_h // 2
        for i, particle in enumerate(self._particles[:10]):
            angle = particle["phase"] + t * particle["speed"]
            radius = self.pet_w * (0.45 + 0.2 * (0.5 + 0.5 * math.sin(t * 3 + i)))
            x = cx + math.cos(angle) * radius
            y = cy + math.sin(angle) * radius * 0.7
            size = particle["size"] * (0.8 + 0.4 * math.sin(t * 6 + i))
            draw.line((x - size, y, x + size, y), fill=(*color, 220), width=2)
            draw.line((x, y - size, x, y + size), fill=(*color, 220), width=2)
        canvas.alpha_composite(layer)

    def _draw_mic(self, canvas: Image.Image, color: tuple[int, int, int], t: float) -> None:
        layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(layer)
        cx = self.pet_x + self.pet_w // 2
        cy = self.pet_y + self.pet_h // 2
        for i in range(3):
            phase = (t * 0.8 + i / 3) % 1.0
            radius = int(self.pet_w * (0.45 + phase * 0.22))
            alpha = int(150 * (1 - phase))
            draw.ellipse((cx - radius, cy - radius, cx + radius, cy + radius),
                         outline=(*color, max(0, alpha)), width=2)
        canvas.alpha_composite(layer)

    def _draw_approval(self, canvas: Image.Image, color: tuple[int, int, int], t: float) -> None:
        layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(layer)
        x0, y0 = self.pet_x - 10, self.pet_y - 10
        x1, y1 = self.pet_x + self.pet_w + 10, self.pet_y + self.pet_h + 4
        pulse = 0.5 + 0.5 * math.sin(t * 4)
        draw.rounded_rectangle((x0, y0, x1, y1), radius=26,
                               outline=(*color, int(120 + 100 * pulse)), width=3)
        canvas.alpha_composite(layer)

    # ------------------------------------------------------------------ bubble
    def _draw_bubble(self, canvas: Image.Image, text: str,
                     color: tuple[int, int, int]) -> None:
        if not text:
            return
        font = self._font(14)
        max_width = min(self.width - 26, 340) - 24
        lines = self._wrap(text, font, max_width, max_lines=6)
        if not lines:
            return
        line_h = 20
        box = font.getbbox("Ag")
        text_h = box[3] - box[1]
        bubble_w = max(int(font.getlength(_shape_text(line))) for line in lines) + 24
        bubble_w = max(90, min(bubble_w, max_width + 24))
        bubble_h = len(lines) * line_h + 16
        x0 = (self.width - bubble_w) // 2
        y1 = self.pet_y - 8
        y0 = max(6, y1 - bubble_h)
        if y0 == 6:
            y1 = y0 + bubble_h
        layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(layer)
        draw.rounded_rectangle((x0, y0, x0 + bubble_w, y1), radius=14,
                               fill=(15, 18, 28, 238),
                               outline=(*color, 210), width=2)
        # tail
        tail_x = x0 + bubble_w // 2
        draw.polygon([(tail_x - 9, y1 - 1), (tail_x + 9, y1 - 1),
                      (tail_x, y1 + 12)], fill=(15, 18, 28, 238))
        draw.line((tail_x - 9, y1, tail_x, y1 + 11, tail_x + 9, y1),
                  fill=(*color, 210), width=2)
        for index, line in enumerate(lines):
            shown = _shape_text(line)
            draw.text((x0 + bubble_w // 2, y0 + 8 + index * line_h),
                      shown, font=font, fill=(246, 249, 255, 255), anchor="ma")
        canvas.alpha_composite(layer)

    # ------------------------------------------------------------------ frame
    def render(self, state: str, text: str = "", t: float = 0.0) -> Image.Image:
        state = state if state in ALL_STATES else IDLE
        meta = STATE_META[state]
        color = _hex_rgb(meta["color"])
        canvas = Image.new("RGBA", (self.width, self.height), (0, 0, 0, 0))

        self._draw_shadow(canvas)
        self._draw_glow(canvas, color, t, 1.35 if state in (SUCCESS, ERROR, APPROVAL) else 1.0)

        if state == RESEARCHING:
            self._draw_orbit(canvas, color, t)
        elif state == CODING:
            self._draw_code(canvas, color, t)
        elif state == EXECUTING:
            self._draw_sparks(canvas, color, t)

        bob = math.sin(t * 2.0) * 3.5
        if state == THINKING:
            bob = math.sin(t * 3.2) * 2.0
        if state == SPEAKING:
            bob = math.sin(t * 10.0) * 1.2
        shake = 0.0
        if state == ERROR:
            shake = math.sin(t * 38) * 3.0
        angle = math.sin(t * 1.5) * 1.6
        if state in (EXECUTING, CODING):
            angle = math.sin(t * 3.0) * 2.2
        pet_img = self._rotated(angle)
        if state == SPEAKING:
            squash = 1.0 + 0.012 * math.sin(t * 12)
            pet_img = pet_img.resize(
                (pet_img.width, max(1, int(pet_img.height * squash))), Image.BILINEAR)
        canvas.alpha_composite(pet_img, (self.pet_x + int(shake),
                                         self.pet_y + int(bob)))

        if state == THINKING:
            self._draw_dots(canvas, color, t)
        elif state == WORKING:
            self._draw_gear(canvas, color, t)
        elif state == SPEAKING:
            self._draw_waves(canvas, color, t)
        elif state == LISTENING:
            self._draw_mic(canvas, color, t)
        elif state == SUCCESS:
            self._draw_sparkle(canvas, color, t)
        elif state == ERROR:
            self._draw_alert(canvas, color, t)
        elif state == APPROVAL:
            self._draw_approval(canvas, color, t)

        if self.show_state_label:
            label = str(meta.get("label_ar") or meta.get("label_en") or state)
            self._pill(canvas, (self.width // 2, self.height - 16), label,
                       fill=(10, 14, 24, 200), fg=(*color, 255),
                       font_size=12, bold=True)

        self._draw_bubble(canvas, text, color)
        return canvas


def render_contact_sheet(master_path: Path, out_dir: Path,
                         states: Iterable[str] = ALL_STATES,
                         width: int = 380, height: int = 470) -> list[Path]:
    """Render every state for visual review without opening a window."""
    out_dir.mkdir(parents=True, exist_ok=True)
    renderer = PetRenderer(master_path, width, height)
    samples = {
        IDLE: "",
        LISTENING: "أتسمعك الآن…",
        THINKING: "أفكر في طلبك",
        RESEARCHING: "أبحث في المصادر",
        CODING: "أكتب التعديلات",
        EXECUTING: "أنفّذ على الجهاز",
        WORKING: "أشتغل على المهمة",
        SPEAKING: "هذه معاينة لصوت OTHVM",
        SUCCESS: "تم التنفيذ بنجاح",
        ERROR: "تعذّر إكمال الطلب",
        APPROVAL: "موافقة مطلوبة قبل المتابعة",
    }
    paths: list[Path] = []
    frames = []
    for index, state in enumerate(states):
        frame = renderer.render(state, samples.get(state, ""), 1.4 + index * 0.4)
        path = out_dir / f"state_{state.lower()}.png"
        frame.save(path)
        paths.append(path)
        frames.append(frame)
    # Contact sheet on a dark background for quick review.
    cols = 4
    rows = (len(frames) + cols - 1) // cols
    sheet = Image.new("RGBA", (cols * width, rows * height), (20, 24, 34, 255))
    for index, frame in enumerate(frames):
        sheet.alpha_composite(frame, ((index % cols) * width,
                                      (index // cols) * height))
    sheet_path = out_dir / "contact_sheet.png"
    sheet.convert("RGB").save(sheet_path, quality=92)
    paths.append(sheet_path)
    return paths
