from src.paths import TEAMDUMP_DIR, PLAYER_CARDS_DIR


def find_team_overview_images() -> list:
    """
    Search common folders for saved team overview graphics.
    """
    search_roots = [
        TEAMDUMP_DIR,
        PLAYER_CARDS_DIR,
    ]

    matches = []

    for root in search_roots:
        if not root.exists():
            continue

        for pattern in ["*roster*.png", "*overview*.png", "*ovr*.png"]:
            matches.extend(root.rglob(pattern))

    return sorted(matches)


def list_team_overview_labels() -> list[str]:
    """
    Return overview image paths as strings for Streamlit.
    """
    paths = find_team_overview_images()

    return [str(path) for path in paths]
