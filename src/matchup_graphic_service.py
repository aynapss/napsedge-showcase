from src.paths import GAMEDAY_COMPS_DIR


def list_matchup_folders() -> list[str]:
    """
    Return all folders inside Gameday Comps.
    """
    if not GAMEDAY_COMPS_DIR.exists():
        return []

    folders = []

    for folder in GAMEDAY_COMPS_DIR.iterdir():
        if folder.is_dir():
            folders.append(folder.name)

    return sorted(folders)


def list_matchup_images(matchup_folder: str) -> list[str]:
    """
    Return all images inside one matchup folder.

    This searches subfolders too.
    """
    folder = GAMEDAY_COMPS_DIR / matchup_folder

    if not folder.exists():
        return []

    images = []

    for pattern in ["*.png", "*.jpg", "*.jpeg"]:
        images.extend(folder.rglob(pattern))

    return sorted(str(path) for path in images)
