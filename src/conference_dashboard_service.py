from src.paths import CONFERENCE_RATINGS_DIR


def list_conference_dashboards() -> list[str]:
    """
    Return all saved conference dashboard PNG files.
    """
    if not CONFERENCE_RATINGS_DIR.exists():
        return []

    files = CONFERENCE_RATINGS_DIR.glob("*_dashboard.png")

    return sorted(file.name for file in files)


def get_conference_dashboard_path(filename: str):
    """
    Return full path for a selected dashboard image.
    """
    path = CONFERENCE_RATINGS_DIR / filename

    if not path.exists():
        return None

    return path
