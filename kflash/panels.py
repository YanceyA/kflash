"""Panel rendering primitives for TUI screens.

Pure functions that produce bordered panels, two-column layouts, spaced-letter
headers, and step dividers — all returning multi-line strings ready for print().

Uses ANSI-aware width calculations from kflash.ansi to ensure correct alignment
even when content contains color escape sequences.
"""

from __future__ import annotations

from kflash.ansi import (
    display_width,
    get_terminal_width,
    pad_to_width,
    strip_ansi,
    supports_unicode,
)
from kflash.theme import get_theme

# ---------------------------------------------------------------------------
# Box-drawing characters (rounded corners)
# ---------------------------------------------------------------------------

BOX_ROUNDED: dict[str, str] = {
    "tl": "\u256d",  # ╭
    "tr": "\u256e",  # ╮
    "bl": "\u2570",  # ╰
    "br": "\u256f",  # ╯
    "h": "\u2500",  # ─
    "v": "\u2502",  # │
}

MAX_PANEL_WIDTH = 80


# ---------------------------------------------------------------------------
# Panel rendering
# ---------------------------------------------------------------------------


def _spaced_header(text: str) -> str:
    """Convert *text* to spaced uppercase letters in brackets.

    Example: ``"devices"`` -> ``"[ D E V I C E S ]"``
    """
    spaced = " ".join(text.upper())
    return f"[ {spaced} ]"


def render_panel(
    header: str,
    content_lines: list[str],
    max_width: int = MAX_PANEL_WIDTH,
    padding: int = 2,
    min_width: int = 0,
) -> str:
    """Render a bordered panel with a spaced-letter header.

    Args:
        header: Header text (will be uppercased and spaced).
        content_lines: Lines of content (may contain ANSI codes).
        max_width: Maximum panel width in columns.
        padding: Horizontal padding inside the panel borders.
        min_width: Minimum inner width (0 = no minimum).

    Returns:
        Multi-line string with rounded Unicode borders.
    """
    theme = get_theme()
    b = BOX_ROUNDED

    # Build header display string (plain first for width calc)
    header_plain = _spaced_header(header)
    header_display = f"{theme.header}{header_plain}{theme.reset}"

    # Calculate inner width from content
    max_content_w = 0
    for line in content_lines:
        w = display_width(line)
        if w > max_content_w:
            max_content_w = w

    header_plain_w = display_width(header_plain)
    min_inner = max(max_content_w + 2 * padding, header_plain_w + 2, min_width)
    inner_width = min(min_inner, max_width - 2)
    # Ensure header fits
    if inner_width < header_plain_w + 2:
        inner_width = header_plain_w + 2

    lines: list[str] = []

    # Top border: ╭[ H E A D E R ]────────╮
    remaining = inner_width - header_plain_w
    top_fill = b["h"] * remaining
    lines.append(
        f"{theme.border}{b['tl']}{theme.reset}"
        f"{header_display}"
        f"{theme.border}{top_fill}{b['tr']}{theme.reset}"
    )

    # Content lines: │  content padded  │
    for line in content_lines:
        content_width = max(inner_width - 2 * padding, 0)
        if content_width == 0:
            visible_line = ""
        else:
            visible_line = line
            if display_width(visible_line) > content_width:
                plain = strip_ansi(visible_line)
                if content_width <= 3:
                    visible_line = plain[:content_width]
                else:
                    visible_line = plain[: content_width - 3] + "..."
        padded = " " * padding + pad_to_width(visible_line, content_width) + " " * padding
        lines.append(
            f"{theme.border}{b['v']}{theme.reset}{padded}{theme.border}{b['v']}{theme.reset}"
        )

    # Empty panel: add one blank line
    if not content_lines:
        blank = " " * inner_width
        lines.append(
            f"{theme.border}{b['v']}{theme.reset}{blank}{theme.border}{b['v']}{theme.reset}"
        )

    # Bottom border: ╰────────────────────╯
    bottom_fill = b["h"] * inner_width
    lines.append(f"{theme.border}{b['bl']}{bottom_fill}{b['br']}{theme.reset}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Two-column layout
# ---------------------------------------------------------------------------


def render_two_column(items: list[tuple[str, str]], gap: int = 4) -> list[str]:
    """Split items into two balanced columns with adaptive widths.

    Args:
        items: List of ``(number, label)`` tuples, e.g. ``("#1", "Flash Device")``.
        gap: Number of spaces between columns.

    Returns:
        List of formatted lines (no borders).
    """
    if not items:
        return []

    theme = get_theme()

    # Format each item
    def fmt(number: str, label: str) -> str:
        return f"{theme.label}{number}{theme.reset} {theme.subtle}\u25b8{theme.reset} {label}"

    formatted = [fmt(n, label) for n, label in items]

    if len(items) == 1:
        return [formatted[0]]

    mid = (len(items) + 1) // 2
    left = formatted[:mid]
    right = formatted[mid:]

    # Calculate left column width
    left_width = max(display_width(s) for s in left)

    result: list[str] = []
    for i, left_item in enumerate(left):
        line = pad_to_width(left_item, left_width)
        if i < len(right):
            line += " " * gap + right[i]
        result.append(line)

    return result


# ---------------------------------------------------------------------------
# Table picker
# ---------------------------------------------------------------------------


def render_table_picker(
    pairs: list,
    selected_index: int | None = None,
    max_width: int = 72,
) -> list[str]:
    """Render a numbered table of flash method pairs.

    Multi-line layout per entry:
      Line 1: "  1. Katapult USB"          (or "▸ 1. Katapult USB" if selected)
      Line 2+: "     Description  (Notes)"  (wrapped to max_width, lavender)

    Blank line between entries for visual separation.
    Selected entry shows ▸ indicator (no highlight).

    Args:
        pairs: List of objects with name, description, notes attributes.
        selected_index: 0-based index of current selection (None = no selection).
        max_width: Maximum visible width (default 72).

    Returns:
        List of formatted lines (no border -- caller wraps if desired).
    """
    theme = get_theme()
    lines: list[str] = []
    indicator = "\u25b8"  # ▸ right-pointing triangle

    for i, pair in enumerate(pairs):
        is_selected = selected_index is not None and i == selected_index

        # Line 1: indicator + number + name
        if is_selected:
            prefix = f"{indicator} {i + 1}. "
        else:
            prefix = f"  {i + 1}. "

        name_line = f"{prefix}{pair.name}"

        # Description lines: indented, wrapped, lavender colored
        # Indent aligns under the name (after "  N. ")
        indent = " " * len(prefix)
        desc_text = pair.description
        if pair.notes:
            desc_text = f"{pair.description}  ({pair.notes})"

        # Wrap description to fit within max_width
        available = max_width - len(indent)
        desc_lines = _wrap_text(desc_text, available)

        lines.append(name_line)
        for dline in desc_lines:
            lines.append(
                f"{indent}{theme.key_info}{dline}{theme.reset}"
            )

        # Blank line between entries (not after last)
        if i < len(pairs) - 1:
            lines.append("")

    return lines


def _wrap_text(text: str, width: int) -> list[str]:
    """Wrap text to fit within width, breaking at word boundaries.

    Returns a list of lines. Never returns an empty list -- at minimum
    returns a single empty-string line.
    """
    if not text or width <= 0:
        return [""]

    words = text.split()
    if not words:
        return [""]

    result_lines: list[str] = []
    current_line = words[0]

    for word in words[1:]:
        if len(current_line) + 1 + len(word) <= width:
            current_line += " " + word
        else:
            result_lines.append(current_line)
            current_line = word

    result_lines.append(current_line)
    return result_lines


# ---------------------------------------------------------------------------
# Step divider
# ---------------------------------------------------------------------------


def render_step_divider(label: str, total_width: int | None = None) -> str:
    """Render a partial-width dashed line with centered label.

    Args:
        label: Text to center in the divider.
        total_width: Total character width (auto-detected if None).

    Returns:
        Single formatted line.
    """
    theme = get_theme()
    if total_width is None:
        total_width = get_terminal_width()
    dash = "\u2504" if supports_unicode() else "-"  # ┄ or -

    label_text = f" {label} "
    label_width = len(label_text)
    side = (total_width - label_width) // 2
    if side < 0:
        side = 0

    left_dashes = dash * side
    right_dashes = dash * (total_width - label_width - side)

    return (
        f"{theme.border}{left_dashes}{theme.reset}"
        f"{theme.dim}{label_text}{theme.reset}"
        f"{theme.border}{right_dashes}{theme.reset}"
    )


def render_action_divider(label: str = "") -> str:
    """Render a divider line to separate action output from menu.

    Args:
        label: Optional text to center in the divider. If provided, uses
               render_step_divider. If empty, produces a simple dashed line.

    Returns:
        Single formatted line.
    """
    if label:
        return render_step_divider(label)

    theme = get_theme()
    dash = "\u2504" if supports_unicode() else "-"  # ┄ or -
    width = get_terminal_width()
    return f"{theme.border}{dash * width}{theme.reset}"


def render_device_divider(index: int, total: int, name: str, total_width: int | None = None) -> str:
    """Render a labeled device divider: --- 1/3 DeviceName ---

    Args:
        index: 1-based device index.
        total: Total number of devices.
        name: Device display name.
        total_width: Override width (auto-detected if None).

    Returns:
        Single formatted line.
    """
    theme = get_theme()
    if total_width is None:
        total_width = get_terminal_width()
    dash = "\u2500" if supports_unicode() else "-"  # ─ or -
    label = f" {index}/{total} {name} "
    label_width = len(label)
    side_left = (total_width - label_width) // 2
    if side_left < 0:
        side_left = 0
    side_right = total_width - label_width - side_left
    if side_right < 0:
        side_right = 0
    return (
        f"{theme.border}{dash * side_left}{theme.reset}"
        f"{theme.border}{label}{theme.reset}"
        f"{theme.border}{dash * side_right}{theme.reset}"
    )


# ---------------------------------------------------------------------------
# Timestamp formatting
# ---------------------------------------------------------------------------


def format_timestamp_relative(iso_str: str) -> str:
    """Format an ISO timestamp as 'YYYY-MM-DD HH:MM (X ago)'.

    Combines absolute date with human-readable relative time.
    Returns the raw string if parsing fails.
    """
    from datetime import datetime

    try:
        dt = datetime.fromisoformat(iso_str)
    except (ValueError, TypeError):
        return iso_str

    now = datetime.now()
    delta = now - dt
    total_seconds = int(delta.total_seconds())

    if total_seconds < 0:
        relative = "in the future"
    elif total_seconds < 60:
        relative = "just now"
    elif total_seconds < 3600:
        minutes = total_seconds // 60
        relative = f"{minutes} minute{'s' if minutes != 1 else ''} ago"
    elif total_seconds < 86400:
        hours = total_seconds // 3600
        relative = f"{hours} hour{'s' if hours != 1 else ''} ago"
    else:
        days = total_seconds // 86400
        relative = f"{days} day{'s' if days != 1 else ''} ago"

    display = dt.strftime("%Y-%m-%d %H:%M")
    return f"{display} ({relative})"


# ---------------------------------------------------------------------------
# Panel centering
# ---------------------------------------------------------------------------


def center_panel(panel_str: str, terminal_width: int | None = None) -> str:
    """Horizontally center a rendered panel in the terminal.

    Args:
        panel_str: Multi-line panel string from render_panel().
        terminal_width: Override terminal width (auto-detected if None).

    Returns:
        Panel string with leading spaces for centering.
    """
    if terminal_width is None:
        terminal_width = get_terminal_width()

    lines = panel_str.split("\n")
    max_w = max((display_width(line) for line in lines), default=0)

    if max_w >= terminal_width:
        return panel_str

    indent = " " * ((terminal_width - max_w) // 2)
    return "\n".join(indent + line for line in lines)
