"""
Shared UI styles and design tokens for OMP Forecasting Dashboard.

This module contains CSS, color palettes, and styling constants
used across the Streamlit web application.
"""

# ============================================================
# DESIGN TOKENS - Color Palette  (light theme)
# ============================================================
COLORS = {
    # Page background
    "bg1": "#f0f2f6",
    "bg2": "#e8eaf0",
    "bg3": "#dde1ea",

    # Panel/Card backgrounds
    "panel": "#ffffff",
    "panel_dark": "#f1f5fb",
    "card_bg": "#ffffff",

    # Text
    "text": "#1a1a2e",
    "text_muted": "#6b7a90",
    "text_dark": "#1a1a2e",

    # Primary accent — navy
    "primary": "#1e3a5f",
    "primary_dark": "#16304f",
    "primary_darker": "#0f2540",
    "primary_darkest": "#0a1a2e",

    # Accent / highlight
    "accent": "#1e3a5f",
    "accent_light": "#4a7ab5",
    "success": "#16a34a",
    "warning": "#d97706",
    "error": "#c0392b",

    # Borders
    "border": "#e2e8f0",
    "border_active": "#c7d2fe",
}

# ============================================================
# FONT SETTINGS
# ============================================================
FONTS = {
    "family": "'Manrope', sans-serif",
    "import_url": "https://fonts.googleapis.com/css2?family=Manrope:wght@400;600;700;800&display=swap",
    "size_base": "14px",
    "size_header": "0.9rem",
    "size_small": "0.85rem",
    "weight_normal": "400",
    "weight_semibold": "600",
    "weight_bold": "700",
    "weight_extrabold": "800",
}

# ============================================================
# SPACING
# ============================================================
SPACING = {
    "xs": "0.25rem",
    "sm": "0.5rem",
    "md": "1rem",
    "lg": "1.5rem",
    "xl": "2rem",
    "xxl": "3rem",
}

# ============================================================
# BORDER RADIUS
# ============================================================
RADIUS = {
    "sm": "4px",
    "md": "8px",
    "lg": "12px",
    "xl": "16px",
    "pill": "9999px",
}


def get_base_css() -> str:
    """Generate the base CSS for the dashboard."""
    return f"""
    <style>
    @import url('{FONTS["import_url"]}');

    :root {{
      --bg1: {COLORS["bg1"]};
      --bg2: {COLORS["bg2"]};
      --bg3: {COLORS["bg3"]};
      --panel: {COLORS["panel"]};
      --text: {COLORS["text"]};
      --accent: {COLORS["accent"]};
      --section-header-size: {FONTS["size_header"]};
    }}

    html, body, [class*="main"] {{
      font-family: {FONTS["family"]};
      background: var(--bg1);
    }}

    .block-container {{
      padding-top: {SPACING["xxl"]};
      padding-bottom: {SPACING["xl"]};
    }}

    /* Sidebar styling */
    section[data-testid="stSidebar"] {{
      background: #ffffff;
      border-right: 1px solid {COLORS["border"]};
    }}

    section[data-testid="stSidebar"] .stMarkdown {{
      color: var(--text);
    }}

    section[data-testid="stSidebar"] h3 {{
      font-size: var(--section-header-size) !important;
    }}

    section[data-testid="stSidebar"] label {{
      color: {COLORS["text"]} !important;
    }}
    </style>
    """


def get_tab_button_css(current_view: str, tab_names: list) -> str:
    """
    Generate CSS for tab button styling based on active tab.

    Args:
        current_view: Name of the currently active tab
        tab_names: List of all tab names in order

    Returns:
        CSS string for tab button styling
    """
    css_parts = ["""
    <style>
    /* Base tab button styles — inactive */
    div.row-widget.stButton > button[kind="secondary"] {
        background-color: #f1f5fb !important;
        color: #374151 !important;
        border: 1px solid #d1d9e6 !important;
    }
    div.row-widget.stButton > button[kind="secondary"]:hover {
        background-color: #e4eaf4 !important;
        border: 1px solid #b8c5d6 !important;
    }
    """]

    # Add specific styling for each tab position
    for i, tab_name in enumerate(tab_names, 1):
        is_active = tab_name == current_view
        if is_active:
            css_parts.append(f"""
    div[data-testid="stHorizontalBlock"] > div:nth-child({i}) button[data-testid="stBaseButton-secondary"] {{
        background-color: {COLORS["primary"]} !important;
        color: #ffffff !important;
        border: none !important;
        font-weight: 700 !important;
    }}
            """)

    css_parts.append("</style>")
    return "\n".join(css_parts)


def get_card_css() -> str:
    """Generate CSS for card/panel components."""
    return f"""
    <style>
    .hero-card {{
      background: {COLORS["panel"]};
      border: 1px solid {COLORS["border"]};
      border-radius: {RADIUS["lg"]};
      padding: {SPACING["lg"]};
      margin-bottom: {SPACING["md"]};
      box-shadow: 0 2px 8px rgba(0,0,0,0.06);
    }}

    .hero-top {{
      display: flex;
      justify-content: space-between;
      align-items: flex-start;
      margin-bottom: {SPACING["md"]};
    }}

    .pill {{
      background: #eef2ff;
      color: {COLORS["primary"]};
      border: 1px solid #c7d2fe;
      padding: {SPACING["xs"]} {SPACING["md"]};
      border-radius: {RADIUS["pill"]};
      font-size: {FONTS["size_small"]};
      font-weight: {FONTS["weight_semibold"]};
      text-transform: uppercase;
      letter-spacing: 0.05em;
    }}

    .hero-meta {{
      text-align: right;
      font-size: {FONTS["size_small"]};
      color: {COLORS["text_muted"]};
    }}

    .hero-title {{
      font-size: 1.8rem;
      font-weight: {FONTS["weight_extrabold"]};
      color: {COLORS["primary"]};
      margin: 0;
    }}
    </style>
    """


def get_table_css() -> str:
    """Generate CSS for table components."""
    return f"""
    <style>
    .queue-wrapper {{
      background: {COLORS["panel"]};
      border: 1px solid {COLORS["border"]};
      border-radius: {RADIUS["lg"]};
      padding: {SPACING["md"]};
      margin-bottom: {SPACING["md"]};
    }}

    .queue-header {{
      display: flex;
      justify-content: space-between;
      font-size: var(--section-header-size);
      margin-bottom: {SPACING["sm"]};
      color: {COLORS["text"]};
    }}

    .queue-row {{
      display: grid;
      padding: {SPACING["sm"]} 0;
      border-bottom: 1px solid {COLORS["border"]};
      font-size: {FONTS["size_small"]};
      color: {COLORS["text"]};
    }}

    .queue-row.queue-head {{
      font-weight: {FONTS["weight_bold"]};
      color: {COLORS["text_muted"]};
      background: {COLORS["panel_dark"]};
      border-radius: {RADIUS["sm"]};
      padding: {SPACING["sm"]};
    }}

    .queue-empty {{
      text-align: center;
      padding: {SPACING["xl"]};
      color: {COLORS["text_muted"]};
      font-style: italic;
    }}
    </style>
    """


def get_expander_css() -> str:
    """Generate CSS for Streamlit expander components."""
    return f"""
    <style>
    .streamlit-expanderHeader {{
      background: {COLORS["panel_dark"]} !important;
      border-radius: {RADIUS["md"]} !important;
      color: {COLORS["text"]} !important;
      font-weight: {FONTS["weight_semibold"]} !important;
    }}

    .streamlit-expanderContent {{
      background: {COLORS["panel"]} !important;
      border-radius: 0 0 {RADIUS["md"]} {RADIUS["md"]} !important;
    }}
    </style>
    """


def get_all_css() -> str:
    """Get all CSS combined into one string."""
    return (
        get_base_css() +
        get_card_css() +
        get_table_css() +
        get_expander_css()
    )


# ============================================================
# HTML TEMPLATE HELPERS
# ============================================================
def render_hero_card(title: str, subtitle: str, meta_lines: list) -> str:
    """
    Render a hero card HTML component.

    Args:
        title: Main title text
        subtitle: Subtitle/pill text
        meta_lines: List of metadata lines to display

    Returns:
        HTML string for the hero card
    """
    meta_html = "".join(f"<div>{line}</div>" for line in meta_lines)
    return f"""
    <div class="hero-card">
      <div class="hero-top">
        <div class="pill">{subtitle}</div>
        <div class="hero-meta">
          {meta_html}
        </div>
      </div>
      <div class="hero-title">{title}</div>
    </div>
    """


def format_number(value: float, decimals: int = 0) -> str:
    """Format a number with thousand separators."""
    if decimals == 0:
        return f"{value:,.0f}"
    return f"{value:,.{decimals}f}"
