"""modkit git — git operations for mod repos."""

import click


@click.group()
@click.option("--game", default=None, help="Game profile (overrides global --game).")
@click.pass_context
def git(ctx, game):
    """Git operations for mod repos: commit, push, pull, init."""
    if game is not None:
        ctx.obj["game"] = game


def _resolve_mod_game(ctx, mod_name: str) -> str:
    """Return game for mod_name: explicit --game flag > .game file > DEFAULT_GAME fallback."""
    if ctx.obj.get("game_explicit"):
        return ctx.obj["game"]
    from app.paths import get_app_root
    root = get_app_root()
    game_file = root / "mods" / mod_name / ".game"
    if game_file.is_file():
        return game_file.read_text(encoding="utf-8").strip()
    return ctx.obj["game"]


def _get_mod_dir(name: str) -> "Path":
    """Resolve mod directory under mods/."""
    from app.paths import get_app_root
    root = get_app_root()
    mod_dir = root / "mods" / name
    if mod_dir.is_dir():
        return mod_dir
    raise click.ClickException(f"Directory not found: {mod_dir}")


def _gitea_creds() -> dict:
    """Read GITEA_USER + GITEA_TOKEN from env/.env for CLI auth."""
    import os
    from app.paths import get_app_root

    def _env(key):
        val = os.environ.get(key, "")
        if val:
            return val
        env_path = get_app_root() / ".env"
        if env_path.is_file():
            for line in env_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line.startswith(f"{key}="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
        return ""

    user = _env("GITEA_USER")
    token = _env("GITEA_TOKEN")
    if user and token:
        return {"gitea_user": user, "gitea_token": token}
    return {}


@git.command()
@click.argument("name")
@click.argument("paths", nargs=-1)
@click.option("--all", "all_files", is_flag=True, help="Explicitly include all changes in this mod repository.")
@click.option("--message", "-m", default=None, help="Commit message (default: timestamped Mod Builder message).")
@click.option("--no-push", is_flag=True, help="Create a local commit without pushing.")
@click.pass_context
def commit(ctx, name, paths, all_files, message, no_push):
    """Commit literal mod-relative PATHS and push. Select PATHS or --all."""
    from creation_lib.mod.git_ops import git_commit
    from cli._output import output

    if bool(paths) == all_files:
        raise click.UsageError("Select one or more mod-relative paths, or --all (not both).")

    mod_dir = _get_mod_dir(name)
    try:
        sha = git_commit(mod_dir, name, paths=list(paths) if paths else None,
                         message=message, push=not no_push, **_gitea_creds())
        output({"mod": name, "commit": sha or None, "paths": list(paths) if paths else None,
                "pushed": bool(sha) and not no_push}, ctx.obj["fmt"])
    except RuntimeError as e:
        raise click.ClickException(str(e))


@git.command()
@click.argument("name")
@click.pass_context
def push(ctx, name):
    """Push to remote."""
    from creation_lib.mod.git_ops import git_push

    mod_dir = _get_mod_dir(name)
    try:
        git_push(mod_dir, **_gitea_creds())
        click.echo("Push complete.")
    except RuntimeError as e:
        raise click.ClickException(str(e))


@git.command()
@click.argument("name")
@click.pass_context
def pull(ctx, name):
    """Pull latest changes from origin."""
    from creation_lib.mod.git_ops import git_pull

    mod_dir = _get_mod_dir(name)
    try:
        git_pull(mod_dir, **_gitea_creds())
        click.echo("Pull complete.")
    except RuntimeError as e:
        raise click.ClickException(str(e))


@git.command()
@click.argument("name")
@click.pass_context
def checkout(ctx, name):
    """Discard all local changes (hard reset + clean)."""
    from creation_lib.mod.git_ops import git_checkout

    mod_dir = _get_mod_dir(name)
    try:
        git_checkout(mod_dir)
        click.echo("Local repository reset to HEAD.")
    except RuntimeError as e:
        raise click.ClickException(str(e))


@git.command()
@click.argument("name")
@click.option("--gitea-url", default=None, help="Gitea server URL")
@click.option("--gitea-user", default=None, help="Gitea username")
@click.option("--gitea-org", default=None, help="Gitea organization (optional)")
@click.option("--gitea-token", default=None, help="Gitea API token (optional)")
@click.pass_context
def init(ctx, name, gitea_url, gitea_user, gitea_org, gitea_token):
    """Initialize git repo and push to Gitea."""
    import os
    from creation_lib.mod.git_ops import gitea_init

    mod_dir = _get_mod_dir(name)
    game = _resolve_mod_game(ctx, name)

    # Fall back to env vars
    def _env(key, override):
        if override is not None:
            return override
        val = os.environ.get(key, "")
        if val:
            return val
        from app.paths import get_app_root
        env_path = get_app_root() / ".env"
        if env_path.is_file():
            for line in env_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line.startswith(f"{key}="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
        return ""

    # Resolve org: explicit flag > GITEA_ORG env > GITEA_ORG_<GAME> from .env.
    # Pass None (not "") as the override so _env falls through to env/.env — an empty-string
    # override short-circuits `if override is not None` and the env lookups never run.
    org = gitea_org or _env("GITEA_ORG", None)
    if not org and game:
        org = _env(f"GITEA_ORG_{game.upper()}", None)

    try:
        gitea_init(
            mod_dir, name,
            game=game,
            gitea_url=_env("GITEA_URL", gitea_url),
            gitea_user=_env("GITEA_USER", gitea_user),
            gitea_org=org,
            gitea_token=_env("GITEA_TOKEN", gitea_token),
            on_progress=click.echo,
        )
        click.echo("Git initialization complete.")
    except RuntimeError as e:
        raise click.ClickException(str(e))
