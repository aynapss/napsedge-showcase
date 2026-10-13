import math
import re
from pathlib import Path

import pandas as pd
from PIL import Image, ImageDraw, ImageFont

from src.paths import CBB_DIR, PLAYER_CARDS_DIR


def list_player_card_teams() -> list[str]:
    """
    Return all team folders inside PlayerCards.
    """
    if not PLAYER_CARDS_DIR.exists():
        return []

    teams = []

    for folder in PLAYER_CARDS_DIR.iterdir():
        if folder.is_dir():
            teams.append(folder.name)

    return sorted(teams)


def list_player_card_images(team_folder: str) -> list[str]:
    """
    Return all image files inside one PlayerCards team folder.
    """
    folder = PLAYER_CARDS_DIR / team_folder

    if not folder.exists():
        return []

    images = []

    for pattern in ["*.png", "*.jpg", "*.jpeg"]:
        images.extend(folder.rglob(pattern))

    return sorted(str(file.relative_to(folder)) for file in images)


def get_player_card_path(team_folder: str, image_name: str):
    """
    Return the full path to a selected player card image.
    """
    path = PLAYER_CARDS_DIR / team_folder / image_name

    if not path.exists():
        return None

    return path


def _safe_name(value: str) -> str:
    text = str(value).lower().strip()
    text = re.sub(r"[^\w\s.-]+", "", text)
    text = re.sub(r"\s+", "_", text)
    return text.strip("._-") or "player"


def _simple_slug(value: str) -> str:
    text = str(value).lower().strip()
    text = text.replace("&", "and")
    text = re.sub(r"[()\"']", "", text)
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return text.strip("-")


def _resolve_team_slug(team_value: str) -> str:
    """
    Convert a PlayerCards folder/display name into a TeamDump/CBB slug.
    """
    raw = str(team_value).strip()
    candidates = [
        raw,
        _simple_slug(raw),
        _simple_slug(raw).replace("saint-", "st-"),
        _simple_slug(raw).replace("st-", "saint-"),
    ]

    manual_aliases = {
        "st-bonaventure": "st-bonaventure",
        "saint-bonaventure": "st-bonaventure",
    }
    candidates.extend(manual_aliases.get(candidate, "") for candidate in list(candidates))

    for candidate in candidates:
        if candidate and (CBB_DIR / candidate).exists():
            return candidate

    return _simple_slug(raw)


def _num(row: pd.Series, key: str, default: float = 0.0) -> float:
    value = row.get(key, default)
    try:
        if pd.isna(value):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _scale(value: float, low: float, high: float) -> int:
    if high <= low:
        return 0
    score = (float(value) - low) / (high - low) * 100
    return int(max(0, min(100, round(score))))


def _tier_from_ovr(ovr: float) -> str:
    if ovr >= 96:
        return "Legendary"
    if ovr >= 80:
        return "Superstar"
    if ovr >= 70:
        return "Starter"
    if ovr >= 60:
        return "Rotation"
    return "Bench"


def _tier_color(tier: str) -> str:
    return {
        "Legendary": "#f97316",
        "Superstar": "#7c3aed",
        "Starter": "#15803d",
        "Rotation": "#2563eb",
        "Bench": "#6b7280",
    }.get(tier, "#2563eb")


def resolve_compact_archetype(primary: str, secondary: str | None, position: str) -> str:
    """
    Converts detected elite traits into a position-aware archetype title.
    Mirrors the notebook's exec 145 replacement without running notebook cells.
    """
    guard_map = {
        ("Scoring", "Playmaking"): "🧠 Offensive Engine",
        ("Playmaking", "Scoring"): "🧠 Offensive Engine",
        ("Scoring", "Defense"): "⚔️ Two-Way Guard",
        ("Defense", "Scoring"): "⚔️ Two-Way Guard",
        ("Scoring", "Shooting"): "🔥 Offensive Weapon",
        ("Shooting", "Scoring"): "🔥 Offensive Weapon",
        ("Playmaking", "Defense"): "⚖️ Two-Way Guard",
        ("Defense", "Playmaking"): "⚖️ Two-Way Guard",
        ("Shooting", "Ball Security"): "🎯 Reliable Shooter",
        ("Ball Security", "Shooting"): "🎯 Reliable Shooter",
        ("Playmaking", "Shooting"): "🎯 Combo Guard",
        ("Shooting", "Playmaking"): "🎯 Combo Guard",
        ("Scoring", "Ball Security"): "🔥 Efficient Scorer",
        ("Ball Security", "Scoring"): "🔥 Efficient Scorer",
        ("Defense", "Rebounding"): "⚔️ Versatile Guard",
        ("Rebounding", "Defense"): "⚔️ Versatile Guard",
        ("Ball Security", "Defense"): "🔩 Glue Guy",
        ("Defense", "Ball Security"): "🔩 Glue Guy",
        ("Ball Security", "Rebounding"): "🔩 Glue Guy",
        ("Rebounding", "Ball Security"): "🔩 Glue Guy",
    }
    wing_map = {
        ("Scoring", "Defense"): "⚔️ Two-Way Wing",
        ("Defense", "Scoring"): "⚔️ Two-Way Wing",
        ("Scoring", "Rebounding"): "💪 Physical Wing",
        ("Rebounding", "Scoring"): "💪 Physical Wing",
        ("Scoring", "Playmaking"): "🧠 Playmaking Wing",
        ("Playmaking", "Scoring"): "🧠 Playmaking Wing",
        ("Scoring", "Shooting"): "🔥 Scoring Wing",
        ("Shooting", "Scoring"): "🔥 Scoring Wing",
        ("Defense", "Rebounding"): "🛡️ Defensive Wing",
        ("Rebounding", "Defense"): "🛡️ Defensive Wing",
        ("Playmaking", "Defense"): "🧠 Connector Wing",
        ("Defense", "Playmaking"): "🧠 Connector Wing",
        ("Shooting", "Defense"): "🎯 3-and-D Wing",
        ("Defense", "Shooting"): "🎯 3-and-D Wing",
        ("Scoring", "Ball Security"): "🔥 Efficient Wing",
        ("Ball Security", "Scoring"): "🔥 Efficient Wing",
    }
    forward_map = {
        ("Scoring", "Rebounding"): "💪 Athletic Forward",
        ("Rebounding", "Scoring"): "💪 Athletic Forward",
        ("Defense", "Rebounding"): "🛡️ Defensive Forward",
        ("Rebounding", "Defense"): "🛡️ Defensive Forward",
        ("Rebounding", "Blocks"): "🧱 Interior Forward",
        ("Blocks", "Rebounding"): "🧱 Interior Forward",
        ("Scoring", "Blocks"): "🚀 Explosive Forward",
        ("Blocks", "Scoring"): "🚀 Explosive Forward",
        ("Scoring", "Playmaking"): "🧠 Point Forward",
        ("Playmaking", "Scoring"): "🧠 Point Forward",
        ("Shooting", "Defense"): "🎯 Stretch Forward",
        ("Defense", "Shooting"): "🎯 Stretch Forward",
        ("Scoring", "Ball Security"): "🔥 Efficient Forward",
        ("Ball Security", "Scoring"): "🔥 Efficient Forward",
    }
    big_map = {
        ("Rebounding", "Blocks"): "🧱 Defensive Anchor",
        ("Blocks", "Rebounding"): "🧱 Defensive Anchor",
        ("Defense", "Rebounding"): "🛡️ Interior Enforcer",
        ("Defense", "Blocks"): "🚫 Defensive Anchor",
        ("Blocks", "Defense"): "🚫 Defensive Anchor",
        ("Scoring", "Rebounding"): "🔥 Interior Force",
        ("Rebounding", "Scoring"): "🔥 Interior Force",
        ("Scoring", "Blocks"): "🔥 Interior Presence",
        ("Blocks", "Scoring"): "🔥 Interior Presence",
        ("Scoring", "Shooting"): "🎯 Stretch Scorer",
        ("Shooting", "Scoring"): "🎯 Stretch Scorer",
        ("Scoring", "Playmaking"): "🧠 Hub Big",
        ("Playmaking", "Scoring"): "🧠 Hub Big",
        ("Defense", "Scoring"): "⚔️ Two-Way Big",
        ("Scoring", "Defense"): "⚔️ Two-Way Big",
    }

    if secondary:
        combo_map = {
            "Guard": guard_map,
            "Wing": wing_map,
            "Forward": forward_map,
            "Big": big_map,
        }.get(position, big_map)
        return combo_map.get((primary, secondary), f"🌟 {primary} {position}")

    single_map = {
        "Scoring": "🔥 Go-To Scorer",
        "Playmaking": "🧠 Primary Creator",
        "Shooting": "🎯 Marksman",
        "Rebounding": "🧱 Glass Dominator",
        "Blocks": "🚫 Rim Guardian",
        "Defense": "🕸️ Disruptor",
        "Ball Security": "🔐 Stabilizer",
    }
    return single_map.get(primary, "🧩 Role Player")


def infer_player_archetype(
    pos_raw: str,
    get_scaled,
    three_pa_40: float,
    final_player_tier: str | None = None,
) -> str:
    """
    Position-gated player archetype resolver from the notebook card pipeline.
    """
    pos_upper = str(pos_raw or "").upper().replace(" ", "")

    if "G" in pos_upper and "F" in pos_upper:
        is_guard, is_forward, is_center = False, True, False
    elif "F" in pos_upper and "C" in pos_upper:
        is_guard, is_forward, is_center = False, False, True
    else:
        is_guard = "G" in pos_upper
        is_forward = "F" in pos_upper and "C" not in pos_upper
        is_center = "C" in pos_upper

    tier_dominance = {
        "Rotation": 1.10,
        "Starter": 1.25,
        "Superstar": 1.45,
        "Legendary": 1.65,
    }
    dominant_threshold = tier_dominance.get(final_player_tier, 1.45)
    elite_traits = []

    if get_scaled("PTS/40") >= dominant_threshold:
        elite_traits.append("Scoring")
    if get_scaled("AST/40") >= dominant_threshold:
        elite_traits.append("Playmaking")

    volume_gate = 2.0 if is_guard else 1.2
    if get_scaled("3FG%") >= dominant_threshold and three_pa_40 >= volume_gate:
        elite_traits.append("Shooting")

    if get_scaled("TO%") <= (2.0 - dominant_threshold):
        elite_traits.append("Ball Security")
    if not is_guard and get_scaled("REB/40") >= dominant_threshold:
        elite_traits.append("Rebounding")
    if not is_guard:
        blk_threshold = dominant_threshold if is_center else dominant_threshold * 1.10
        if get_scaled("BLK/40") >= blk_threshold:
            elite_traits.append("Blocks")
    if get_scaled("STL/40") >= dominant_threshold:
        elite_traits.append("Defense")

    apex_guard = {
        frozenset(["Scoring", "Playmaking", "Defense"]): "🧠 Complete Guard",
        frozenset(["Scoring", "Playmaking", "Shooting"]): "🌟 Offensive Hub",
        frozenset(["Scoring", "Shooting", "Ball Security"]): "🔥 All-Around Scorer",
        frozenset(["Scoring", "Playmaking", "Ball Security"]): "🎯 Efficient Lead Guard",
        frozenset(["Playmaking", "Defense", "Ball Security"]): "⚖️ Defensive Organizer",
        frozenset(["Scoring", "Defense", "Ball Security"]): "🔥 Lockdown Scorer",
        frozenset(["Scoring", "Playmaking", "Defense", "Shooting"]): "🌟 All-Around Guard",
        frozenset(["Scoring", "Playmaking", "Shooting", "Ball Security"]): "🌟 Elite Guard",
    }
    apex_wing = {
        frozenset(["Scoring", "Defense", "Rebounding"]): "⚔️ Two-Way Force",
        frozenset(["Scoring", "Playmaking", "Defense"]): "🧠 Complete Wing",
        frozenset(["Scoring", "Shooting", "Defense"]): "⚖️ 3-and-D Scorer",
        frozenset(["Scoring", "Playmaking", "Shooting"]): "🌟 Offensive Threat",
        frozenset(["Scoring", "Rebounding", "Shooting"]): "🔥 Power Wing",
        frozenset(["Scoring", "Playmaking", "Defense", "Shooting"]): "🌟 Elite Wing",
    }
    apex_forward = {
        frozenset(["Scoring", "Defense", "Rebounding"]): "⚔️ Two-Way Forward",
        frozenset(["Scoring", "Playmaking", "Defense"]): "🧠 Complete Forward",
        frozenset(["Scoring", "Playmaking", "Rebounding"]): "💪 All-Around Forward",
        frozenset(["Defense", "Rebounding", "Blocks"]): "🧱 Defensive Pillar",
        frozenset(["Scoring", "Rebounding", "Blocks"]): "🚀 Dominant Forward",
        frozenset(["Scoring", "Defense", "Rebounding", "Playmaking"]): "🌟 Complete Forward",
    }
    apex_big = {
        frozenset(["Rebounding", "Blocks", "Scoring"]): "🚫 Interior Force",
        frozenset(["Rebounding", "Blocks", "Defense"]): "🧱 Defensive Anchor",
        frozenset(["Scoring", "Rebounding", "Playmaking"]): "🧠 Versatile Big",
        frozenset(["Scoring", "Shooting", "Rebounding"]): "🎯 Stretch Big",
        frozenset(["Scoring", "Blocks", "Defense"]): "🚫 Dominant Interior",
        frozenset(["Scoring", "Rebounding", "Defense"]): "⚔️ Two-Way Big",
        frozenset(["Rebounding", "Defense", "Scoring"]): "⚔️ Two-Way Big",
        frozenset(["Scoring", "Rebounding", "Blocks", "Defense"]): "🌟 Complete Big",
        frozenset(["Rebounding", "Blocks", "Scoring", "Shooting"]): "🔥 Stretch Anchor",
    }

    if final_player_tier in {"Superstar", "Legendary"} and len(elite_traits) >= 3:
        trait_set = frozenset(elite_traits)
        if is_guard:
            apex_dict = apex_guard
        elif is_forward:
            apex_dict = apex_wing if three_pa_40 >= 3.0 or get_scaled("STL/40") >= 1.15 else apex_forward
        else:
            apex_dict = apex_big

        for combo, label in apex_dict.items():
            if combo.issubset(trait_set):
                return label

    if final_player_tier in {"Superstar", "Legendary"} and not is_guard:
        interior_hits = len({"Scoring", "Rebounding", "Blocks"}.intersection(elite_traits))
        if interior_hits >= 2 and get_scaled("PTS/40") >= dominant_threshold:
            return "🚫 Interior Force" if is_forward else "🚫 Interior Anchor"

    if len(elite_traits) >= 2:
        if is_guard:
            priority = ["Playmaking", "Scoring", "Shooting", "Defense", "Ball Security", "Rebounding", "Blocks"]
            position = "Guard"
        elif is_forward:
            priority = ["Scoring", "Defense", "Rebounding", "Playmaking", "Shooting", "Blocks", "Ball Security"]
            position = "Wing" if three_pa_40 >= 3.0 or get_scaled("STL/40") >= 1.15 else "Forward"
        else:
            priority = ["Blocks", "Rebounding", "Defense", "Scoring", "Shooting", "Playmaking", "Ball Security"]
            position = "Big"
        sorted_traits = sorted(elite_traits, key=lambda trait: priority.index(trait) if trait in priority else 99)
        return resolve_compact_archetype(sorted_traits[0], sorted_traits[1], position)

    if len(elite_traits) == 1:
        trait = elite_traits[0]
        if trait == "Shooting" and not is_guard and get_scaled("3FG%") >= get_scaled("PTS/40") + 0.15:
            return "🎯 Stretch 4" if is_forward else "🎯 Stretch 5"
        if is_guard:
            position = "Guard"
        elif is_forward:
            position = "Wing" if three_pa_40 >= 3.0 or get_scaled("STL/40") >= 1.15 else "Forward"
        else:
            position = "Big"
        return resolve_compact_archetype(trait, None, position)

    if is_guard:
        if get_scaled("AST/40") >= 1.45 and three_pa_40 >= 1.5:
            return "🧠 Floor General"
        if get_scaled("3FG%") >= 1.10 and get_scaled("AST/40") >= 1.30:
            return "🎯 Combo Guard"
        if get_scaled("PTS/40") >= 1.30:
            return "🔥 Shot Creator"
        if get_scaled("3FG%") >= 1.10 and get_scaled("AST/40") < 1.20:
            return "🎯 Spot-Up Shooter"
        if (
            get_scaled("TO%") >= 1.35
            and get_scaled("STL/40") >= 1.20
            and get_scaled("REB/40") >= 1.15
            and get_scaled("PTS/40") >= 1.00
        ):
            return "🔩 Glue Guy"
        if get_scaled("STL/40") >= 1.25:
            return "🕸️ Defensive Guard"
        return "🧩 Role Guard"

    if is_forward:
        is_wing_forward = three_pa_40 >= 3.0 or get_scaled("STL/40") >= 1.15
        if is_wing_forward:
            if get_scaled("PTS/40") >= 1.30:
                return "🎯 Scoring Wing"
            if get_scaled("3FG%") >= 1.05 and get_scaled("STL/40") >= 1.10:
                return "🎯 3-and-D Wing"
            if get_scaled("3FG%") >= 1.05 and get_scaled("REB/40") >= 1.25:
                return "🧠 Two-Way Wing"
            if get_scaled("3FG%") >= 1.10 and get_scaled("PTS/40") < 1.25:
                return "🎯 Floor Spacer"
            if get_scaled("STL/40") >= 1.20:
                return "⚔️ Defensive Wing"
            return "🧩 Role Wing"
        if get_scaled("PTS/40") >= 1.30 and get_scaled("REB/40") >= 1.20:
            return "💪 Athletic Forward"
        if get_scaled("AST/40") >= 1.25:
            return "🧠 Playmaking Forward"
        if get_scaled("REB/40") >= 1.30 and get_scaled("BLK/40") >= 1.10:
            return "🧱 Interior Forward"
        if get_scaled("BLK/40") >= 1.20:
            return "🚀 Explosive Forward"
        if get_scaled("REB/40") >= 1.30:
            return "🛡️ Defensive Forward"
        return "🧩 Role Forward"

    if is_center:
        if get_scaled("BLK/40") >= 1.45 and get_scaled("REB/40") >= 1.35:
            return "🚫 Defensive Anchor"
        if get_scaled("BLK/40") >= 1.45:
            return "🚫 Rim Protector"
        if get_scaled("REB/40") >= 1.45:
            return "🧹 Glass Cleaner"
        if get_scaled("3FG%") >= 1.20 and three_pa_40 >= 1.2:
            return "🎯 Stretch Big"
        if get_scaled("PTS/40") >= 1.25:
            return "🔥 Interior Finisher"
        if get_scaled("STL/40") >= 1.20 or get_scaled("BLK/40") >= 1.25:
            return "🛡️ Defensive Big"
        return "🧱 Rotation Big"

    return "🧩 Role Player"


def role_aware_logistic_curve(composite_score: float, role: str) -> float:
    if role in {"Bench", "Rotation"}:
        m, k = 1.075, 5.8
    elif role == "Starter":
        m, k = 1.12, 6.2
    elif role == "Legendary":
        m, k = 1.16, 6.8
    else:
        m, k = 1.14, 6.5

    ovr = 50.0 + 50.0 / (1.0 + math.exp(-k * (composite_score - m)))
    return round(float(max(0.0, min(100.0, ovr))), 1)


def format_badges(badges, per_line: int = 4) -> str:
    if not badges:
        return ""
    lines = [
        " · ".join(badges[i:i + per_line])
        for i in range(0, len(badges), per_line)
    ]
    return "\n".join(lines)


def _load_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    font_names = [
        "C:/Windows/Fonts/consolab.ttf" if bold else "C:/Windows/Fonts/consola.ttf",
        "C:/Windows/Fonts/arialbd.ttf" if bold else "C:/Windows/Fonts/arial.ttf",
    ]
    for name in font_names:
        try:
            return ImageFont.truetype(name, size=size)
        except OSError:
            continue
    return ImageFont.load_default()


def _load_player_source(team_slug: str, season: int) -> pd.DataFrame:
    team_dir = CBB_DIR / team_slug
    all_stats_path = team_dir / f"{team_slug}_{season}_AllStats.xlsx"
    ovr_path = team_dir / f"{team_slug}_{season}_ovr_cache.csv"

    if not all_stats_path.exists():
        return pd.DataFrame()

    all_stats = pd.read_excel(all_stats_path)

    if ovr_path.exists():
        ovr = pd.read_csv(ovr_path)
        all_stats = all_stats.merge(
            ovr,
            how="left",
            on="Player",
            suffixes=("", "_OVR"),
        )

    if "OVR" not in all_stats.columns:
        all_stats["OVR"] = all_stats.get("PlayerValueIndex", 0).fillna(0) * 7 + 50

    return all_stats


def list_generation_teams() -> list[str]:
    """
    Return teams that have local TeamDump/CBB data available for card generation.
    """
    if not CBB_DIR.exists():
        return []
    return sorted(folder.name for folder in CBB_DIR.iterdir() if folder.is_dir())


def list_players_for_generation(team_value: str, season: int) -> list[str]:
    """
    Return player names that can be generated from local team stats.
    """
    team_slug = _resolve_team_slug(team_value)
    df = _load_player_source(team_slug, season)
    if df.empty or "Player" not in df.columns:
        return []
    return sorted(df["Player"].dropna().astype(str).unique())


def _pct_value(value: float) -> float:
    """
    Normalize percentage columns that may arrive as 0.545 or 54.5.
    """
    if value > 1.5:
        return value / 100.0
    return value


def _first_num(row: pd.Series, keys: list[str], default: float = 0.0) -> float:
    for key in keys:
        if key in row:
            value = _num(row, key, default)
            if value != default:
                return value
    return default


def _draw_label(value: str) -> str:
    """
    PIL's default Windows fonts often cannot render emoji. Keep emoji in model
    labels, but strip them from raster text so the exported card stays clean.
    """
    return str(value).encode("ascii", "ignore").decode("ascii").strip()


def _three_pa_40(row: pd.Series) -> float:
    total_minutes = _first_num(row, ["MP_Totals", "MP", "Minutes"], 0.0)
    mpg = _first_num(row, ["MPG", "MP_PerGame"], 0.0)
    gp = _first_num(row, ["GP", "G"], 0.0)

    attempts_total = _first_num(row, ["3PA_Totals", "3FGA"], 0.0)
    totals_estimate = 0.0
    if attempts_total > 0 and total_minutes > 0:
        totals_estimate = attempts_total / total_minutes * 40.0

    attempts_pg = _first_num(row, ["3PA_PerGame", "3PA", "3PAPG"], 0.0)
    pergame_estimate = 0.0
    if attempts_pg > 0 and mpg > 0:
        pergame_estimate = attempts_pg / mpg * 40.0

    if totals_estimate > 0 and pergame_estimate > 0:
        ratio = totals_estimate / pergame_estimate
        return min(9.0, pergame_estimate if ratio > 1.5 or ratio < 0.67 else totals_estimate)
    if totals_estimate > 0:
        return min(9.0, totals_estimate)
    if pergame_estimate > 0:
        return min(9.0, pergame_estimate)

    per40_col = _first_num(row, ["3PA_Per40"], 0.0)
    if 0.0 < per40_col < 9.0:
        return per40_col

    if attempts_total > 0 and gp > 0 and mpg > 0:
        return min(9.0, (attempts_total / gp) / mpg * 40.0)

    return 0.0


def _skill_score_from_ratio(value: float) -> int:
    return int(max(0, min(100, round(50 + (float(value) - 1.0) * 50))))


def _metric_profile(row: pd.Series) -> tuple[dict[str, float], dict[str, int], float]:
    """
    Build the notebook-style scaled metric profile used by archetypes and bars.

    Ratios are centered on 1.0 as D1 average. That keeps the Streamlit card
    aligned with the notebook's trait thresholds instead of only drawing bars.
    """
    three_pa40 = _three_pa_40(row)
    pts40 = _first_num(row, ["PTS/40", "PTS_Per40"], 0.0)
    ast40 = _first_num(row, ["AST/40", "AST_Per40"], 0.0)
    reb40 = _first_num(row, ["REB/40", "TRB_Per40", "REB_Per40"], 0.0)
    stl40 = _first_num(row, ["STL/40", "STL_Per40"], 0.0)
    blk40 = _first_num(row, ["BLK/40", "BLK_Per40"], 0.0)
    ts_pct = _pct_value(_first_num(row, ["TS%", "TS%_Advanced"], 0.0))
    three_pct = _pct_value(_first_num(row, ["3FG%", "3P%_PerGame", "3P%"], 0.0))
    tov_pct = _first_num(row, ["TO%", "TOV%_Advanced"], 16.5)

    bench = {
        "PTS/40": 14.0,
        "TS%": 0.535,
        "3FG%": 0.345,
        "AST/40": 3.0,
        "REB/40": 6.5,
        "STL/40": 1.2,
        "BLK/40": 0.8,
        "TO%": 16.5,
    }

    shooting_volume = min(1.35, max(0.40, three_pa40 / 4.0)) if three_pa40 >= 0.3 else 0.0
    scaled = {
        "PTS/40": pts40 / bench["PTS/40"] if bench["PTS/40"] else 0.0,
        "TS%": ts_pct / bench["TS%"] if bench["TS%"] else 0.0,
        "3FG%": (three_pct / bench["3FG%"] * shooting_volume) if three_pct > 0 else 0.0,
        "AST/40": ast40 / bench["AST/40"] if bench["AST/40"] else 0.0,
        "REB/40": reb40 / bench["REB/40"] if bench["REB/40"] else 0.0,
        "STL/40": stl40 / bench["STL/40"] if bench["STL/40"] else 0.0,
        "BLK/40": blk40 / bench["BLK/40"] if bench["BLK/40"] else 0.0,
        "TO%": bench["TO%"] / tov_pct if tov_pct > 0 else 1.0,
    }
    scaled = {key: float(max(0.0, min(2.0, value))) for key, value in scaled.items()}

    skills = {
        "Scoring Role": _skill_score_from_ratio(scaled["PTS/40"]),
        "Efficiency": _skill_score_from_ratio(scaled["TS%"]),
        "3PT Impact": _skill_score_from_ratio(scaled["3FG%"]),
        "Playmaking": _skill_score_from_ratio(scaled["AST/40"]),
        "Rebounding": _skill_score_from_ratio(scaled["REB/40"]),
        "Steal Ability": _skill_score_from_ratio(scaled["STL/40"]),
        "Blocks": _skill_score_from_ratio(scaled["BLK/40"]),
        "Ball Security": _skill_score_from_ratio(scaled["TO%"]),
    }
    return scaled, skills, three_pa40


def _build_badges(scaled: dict[str, float], three_pa40: float, pos: str) -> list[str]:
    pos_upper = str(pos or "").upper()
    is_guard = "G" in pos_upper and "C" not in pos_upper
    is_big = "C" in pos_upper or ("F" in pos_upper and three_pa40 < 1.2)

    badges = []
    if scaled.get("PTS/40", 0.0) >= 1.50:
        badges.append("🚀 Elite Scorer")
    elif scaled.get("PTS/40", 0.0) >= 1.25:
        badges.append("🔥 Bucket Getter")

    if scaled.get("TS%", 0.0) >= 1.15:
        badges.append("⚡ Efficient")
    if three_pa40 >= 3.5 and scaled.get("3FG%", 0.0) >= 1.20:
        badges.append("📐 Floor Spacer")
    elif three_pa40 >= 2.0 and scaled.get("3FG%", 0.0) >= 1.10:
        badges.append("🎯 Shooter")

    if scaled.get("AST/40", 0.0) >= 1.35:
        badges.append("🪀 Playmaker")
    if scaled.get("TO%", 0.0) >= 1.35:
        badges.append("🔐 Ball Security")
    if scaled.get("STL/40", 0.0) >= 1.25:
        badges.append("🕷️ Pickpocket")
    if scaled.get("BLK/40", 0.0) >= (1.35 if is_big else 1.55):
        badges.append("🚫 Rim Protector")
    if scaled.get("REB/40", 0.0) >= (1.35 if not is_guard else 1.20):
        badges.append("🧹 Glass Cleaner")
    if (
        is_guard
        and scaled.get("TO%", 0.0) >= 1.30
        and scaled.get("STL/40", 0.0) >= 1.15
        and scaled.get("REB/40", 0.0) >= 1.05
    ):
        badges.append("🔩 Glue Guy")

    return badges[:6]


def generate_player_card(
    team_value: str,
    season: int,
    player_name: str,
    *,
    team_name: str | None = None,
    conference: str = "",
    portal_status: str = "",
    transfer_destination: str = "",
    use_full_engine: bool = True,
) -> Path:
    """
    Generate a NapsEdge-style player radar card from local stats.

    By default this delegates to the extracted notebook engine in src.ovr_model
    so Streamlit uses the same full visual logic as build_composite_radar_v9.
    """
    if use_full_engine:
        from src.ovr_model import build_full_player_card

        output_path, _meta = build_full_player_card(
            team_value,
            season,
            player_name,
            team_name=team_name,
            conference=conference,
            portal_status=portal_status,
            transfer_destination=transfer_destination,
        )
        return output_path

    team_slug = _resolve_team_slug(team_value)
    df = _load_player_source(team_slug, season)

    if df.empty:
        raise FileNotFoundError(f"No AllStats file found for {team_slug}, {season}.")

    matches = df[df["Player"].astype(str) == str(player_name)]
    if matches.empty:
        raise ValueError(f"Player not found in local stats: {player_name}")

    row = matches.iloc[0]
    scaled_metrics, skills, three_pa40 = _metric_profile(row)
    ovr = _num(row, "OVR", 50.0)
    pos = str(row.get("Pos", row.get("Position", "")) or "")
    cls = str(row.get("Class", row.get("Class_OVR", "")) or "")
    tier = _tier_from_ovr(ovr)
    accent = _tier_color(tier)
    get_scaled = lambda metric: scaled_metrics.get(metric, 1.0)
    archetype = infer_player_archetype(pos, get_scaled, three_pa40, tier)
    badges = _build_badges(scaled_metrics, three_pa40, pos)
    badge_text = format_badges(badges, per_line=3)

    width, height = 900, 1380
    image = Image.new("RGB", (width, height), "#f8fafc")
    draw = ImageDraw.Draw(image)

    font_xs = _load_font(18)
    font_sm = _load_font(22)
    font_md = _load_font(30, bold=True)
    font_lg = _load_font(42, bold=True)
    font_xl = _load_font(52, bold=True)

    # Header
    draw.text((48, 28), "NapsEdge Analytics · Sources: Local TeamDump", fill="#0f172a", font=font_xs)
    draw.text((48, 72), str(player_name), fill="#1e3a5f", font=font_xl)
    draw.text((48, 130), f"OVR {ovr:.1f} · {tier}", fill="#2563eb", font=font_md)
    draw.text((48, 168), _draw_label(archetype), fill="#0f172a", font=font_sm)
    draw.text((610, 72), f"{tier} · {pos}", fill=accent, font=font_md)
    draw.text((610, 112), f"{cls} · {season}", fill="#334155", font=font_sm)
    draw.text((610, 146), f"3PA/40: {three_pa40:.1f}", fill="#64748b", font=font_xs)
    if portal_status:
        portal_line = portal_status
        if transfer_destination:
            portal_line += f" · {transfer_destination}"
        draw.text((610, 170), portal_line, fill="#ea580c", font=font_xs)
    if badge_text:
        draw.text((48, 198), f"Badges: {_draw_label(badge_text)}", fill="#475569", font=font_xs)
    draw.line((48, 232, 820, 232), fill="#cbd5e1", width=2)

    # Radar
    labels = list(skills.keys())
    values = list(skills.values())
    cx, cy, radius = 450, 505, 270

    for ring in [25, 50, 75, 100]:
        pts = []
        rr = radius * ring / 100
        for i in range(len(labels)):
            angle = -math.pi / 2 + 2 * math.pi * i / len(labels)
            pts.append((cx + rr * math.cos(angle), cy + rr * math.sin(angle)))
        draw.polygon(pts, outline="#dbe5f1")

    for i, label in enumerate(labels):
        angle = -math.pi / 2 + 2 * math.pi * i / len(labels)
        x = cx + radius * math.cos(angle)
        y = cy + radius * math.sin(angle)
        draw.line((cx, cy, x, y), fill="#e2e8f0", width=1)
        lx = cx + (radius + 48) * math.cos(angle)
        ly = cy + (radius + 48) * math.sin(angle)
        draw.text((lx - 70, ly - 12), label, fill="#0f172a", font=font_xs)

    player_pts = []
    for i, value in enumerate(values):
        angle = -math.pi / 2 + 2 * math.pi * i / len(values)
        rr = radius * value / 100
        player_pts.append((cx + rr * math.cos(angle), cy + rr * math.sin(angle)))
    draw.polygon(player_pts, fill="#ef444455", outline="#ef4444")
    for x, y in player_pts:
        draw.ellipse((x - 5, y - 5, x + 5, y + 5), fill="#ef4444")

    # OVR scale
    scale_y = 820
    draw.text((235, scale_y - 42), "NCAA AVG · 75 OVR", fill="#475569", font=font_xs)
    bands = [
        ("BENCH", "#6b7280", 50, 60),
        ("ROTATION", "#2563eb", 60, 70),
        ("STARTER", "#15803d", 70, 80),
        ("SUPERSTAR", "#7c3aed", 80, 96),
        ("ELITE", "#f97316", 96, 100),
    ]
    bar_x, bar_w, bar_h = 120, 660, 36
    for label, color, low, high in bands:
        x0 = bar_x + int((low - 50) / 50 * bar_w)
        x1 = bar_x + int((high - 50) / 50 * bar_w)
        draw.rectangle((x0, scale_y, x1, scale_y + bar_h), fill=color)
        draw.text((x0 + 8, scale_y + 7), label, fill="white", font=font_xs)
    marker_x = bar_x + int(max(0, min(1, (ovr - 50) / 50)) * bar_w)
    draw.polygon([(marker_x, scale_y + bar_h + 4), (marker_x - 10, scale_y + bar_h + 26), (marker_x + 10, scale_y + bar_h + 26)], fill="#e11d48")
    draw.text((345, scale_y + 70), f"{tier} · OVR {ovr:.1f}", fill=accent, font=font_md)

    # Skill bars
    skill_y = 960
    draw.text((55, skill_y - 36), "SKILL BREAKDOWN", fill="#0f172a", font=font_md)
    for i, (label, value) in enumerate(skills.items()):
        y = skill_y + i * 42
        draw.text((70, y), label, fill="#0f172a", font=font_xs)
        draw.rectangle((285, y + 5, 765, y + 25), fill="#e2e8f0")
        draw.rectangle((285, y + 5, 285 + int(480 * value / 100), y + 25), fill=accent if value >= 60 else "#ef4444")
        draw.text((780, y), str(value), fill=accent if value >= 60 else "#ef4444", font=font_xs)

    # Footer verdict
    draw.rectangle((70, 1280, 830, 1366), fill="#111827")
    top_skills = sorted(skills.items(), key=lambda item: item[1], reverse=True)[:3]
    low_skills = sorted(skills.items(), key=lambda item: item[1])[:2]
    strengths = " · ".join(f"{name} ({score})" for name, score in top_skills)
    limits = " · ".join(f"{name} ({score})" for name, score in low_skills)
    draw.text((95, 1298), "SCOUT'S VERDICT", fill="#f8fafc", font=font_sm)
    draw.text((95, 1324), f"{_draw_label(archetype)} | {strengths}", fill="#34d399", font=font_xs)
    draw.text((95, 1348), f"Watch: {limits}", fill="#fca5a5", font=font_xs)

    output_dir = PLAYER_CARDS_DIR / team_slug
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"{_safe_name(player_name)}_{season}_generated_card.png"
    image.save(output_path)
    return output_path
