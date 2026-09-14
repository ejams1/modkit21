"""Small Tkinter setup window for CLI distributions."""

from __future__ import annotations

from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from app.env_sync import ENV_KEY_MAP, export_settings_to_env, parse_env_file
from creation_lib.core.game_profiles import GAME_PROFILES
from creation_lib.core.path_detector import detect_game_path, validate_game_path
from ui.toolkit.settings import ToolkitSettings


def run_setup_gui(*, force: bool = False) -> bool:
    settings = ToolkitSettings()
    root = tk.Tk()
    if settings.setup_complete and not force:
        if not messagebox.askyesno(
            "ModBox21 Setup",
            "Setup is already complete. Open setup again?",
            parent=root,
        ):
            root.destroy()
            return False

    app = _SetupWindow(root, settings)
    root.mainloop()
    return app.completed


class _SetupWindow:
    def __init__(self, root: tk.Tk, settings: ToolkitSettings):
        self.root = root
        self.settings = settings
        self.completed = False
        self.env_values = parse_env_file()
        # Path config covers every game with .env path keys — including FO76,
        # which is an asset/conversion source (is_moddable=False) but still needs
        # FO76_DIR / FO76_EXTRACTED_DIR configured.
        self.profiles = [p for p in GAME_PROFILES.values() if p.id in ENV_KEY_MAP]
        # Default game is a mod-authoring target, so it stays moddable-only.
        self.moddable_profiles = [p for p in self.profiles if p.is_moddable]
        self.game_vars: dict[str, dict[str, tk.Variable]] = {}

        self.root.title("ModBox21 CLI Setup")
        self.root.geometry("900x520")
        self.root.minsize(760, 420)

        active_game = self.env_values.get("DEFAULT_GAME") or settings.get_active_game()
        if active_game not in {p.id for p in self.moddable_profiles}:
            active_game = "fo4"
        self.active_game = tk.StringVar(value=active_game)
        self.mod_prefix = tk.StringVar(
            value=self.env_values.get("MOD_PREFIX") or settings.mod_prefix or "B21"
        )

        self._build()

    def _build(self) -> None:
        outer = ttk.Frame(self.root, padding=14)
        outer.pack(fill=tk.BOTH, expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(2, weight=1)

        ttk.Label(
            outer,
            text="Configure the CLI once. Setup writes toolkit_settings.json and .env.",
        ).grid(row=0, column=0, sticky="w")

        top = ttk.Frame(outer)
        top.grid(row=1, column=0, sticky="ew", pady=(12, 10))
        top.columnconfigure(1, weight=1)
        top.columnconfigure(3, weight=1)

        ttk.Label(top, text="Default game").grid(row=0, column=0, sticky="w")
        game_combo = ttk.Combobox(
            top,
            textvariable=self.active_game,
            values=[p.id for p in self.moddable_profiles],
            state="readonly",
            width=18,
        )
        game_combo.grid(row=0, column=1, sticky="w", padx=(8, 24))

        ttk.Label(top, text="Mod prefix").grid(row=0, column=2, sticky="w")
        ttk.Entry(top, textvariable=self.mod_prefix, width=18).grid(
            row=0, column=3, sticky="w", padx=(8, 0)
        )

        paths = ttk.LabelFrame(outer, text="Game paths", padding=10)
        paths.grid(row=2, column=0, sticky="nsew")
        paths.columnconfigure(2, weight=1)
        paths.columnconfigure(4, weight=1)

        headers = ["Use", "Game", "Install folder", "", "Extracted data", ""]
        for col, label in enumerate(headers):
            ttk.Label(paths, text=label).grid(row=0, column=col, sticky="w", padx=4)

        for row, profile in enumerate(self.profiles, start=1):
            self._add_game_row(paths, row, profile)

        buttons = ttk.Frame(outer)
        buttons.grid(row=3, column=0, sticky="ew", pady=(12, 0))
        buttons.columnconfigure(0, weight=1)

        ttk.Button(buttons, text="Cancel", command=self.root.destroy).grid(
            row=0, column=1, padx=(0, 8)
        )
        ttk.Button(buttons, text="Save Setup", command=self._save).grid(
            row=0, column=2
        )

    def _add_game_row(self, parent: ttk.Frame, row: int, profile) -> None:
        field_map = ENV_KEY_MAP[profile.id]
        paths = self.settings.get_game_paths(profile.id)
        root_value = (
            self.env_values.get(field_map["root"])
            or paths.get("root_dir", "")
            or detect_game_path(profile.id)
            or ""
        )
        extracted_value = (
            self.env_values.get(field_map["extracted"])
            or paths.get("extracted_dir", "")
            or ""
        )

        enabled = tk.BooleanVar(
            value=bool(root_value) or profile.id == self.active_game.get()
        )
        root_var = tk.StringVar(value=root_value)
        extracted_var = tk.StringVar(value=extracted_value)
        self.game_vars[profile.id] = {
            "enabled": enabled,
            "root": root_var,
            "extracted": extracted_var,
        }

        ttk.Checkbutton(parent, variable=enabled).grid(row=row, column=0, padx=4)
        ttk.Label(parent, text=profile.display_name).grid(
            row=row, column=1, sticky="w", padx=4
        )
        ttk.Entry(parent, textvariable=root_var).grid(
            row=row, column=2, sticky="ew", padx=4, pady=3
        )
        ttk.Button(
            parent,
            text="Browse",
            command=lambda v=root_var: self._browse_dir(v),
        ).grid(row=row, column=3, padx=4)
        ttk.Entry(parent, textvariable=extracted_var).grid(
            row=row, column=4, sticky="ew", padx=4, pady=3
        )
        ttk.Button(
            parent,
            text="Browse",
            command=lambda v=extracted_var: self._browse_dir(v),
        ).grid(row=row, column=5, padx=4)

    def _browse_dir(self, variable: tk.StringVar) -> None:
        initial = variable.get() or str(Path.home())
        selected = filedialog.askdirectory(initialdir=initial)
        if selected:
            variable.set(selected)

    def _save(self) -> None:
        active_game = self.active_game.get()
        if active_game not in self.game_vars:
            messagebox.showerror("Setup Error", "Choose a default game.")
            return

        invalid_roots: list[str] = []
        for profile in self.profiles:
            vars_for_game = self.game_vars[profile.id]
            enabled = bool(vars_for_game["enabled"].get())
            root_dir = str(vars_for_game["root"].get()).strip()
            extracted_dir = str(vars_for_game["extracted"].get()).strip()

            self.settings._paths[profile.id]["root_dir"] = root_dir if enabled else ""
            self.settings._paths[profile.id]["extracted_dir"] = (
                extracted_dir if enabled else ""
            )

            if enabled and root_dir and not validate_game_path(profile.id, root_dir):
                invalid_roots.append(profile.display_name)

        if invalid_roots:
            names = ", ".join(invalid_roots)
            if not messagebox.askyesno(
                "Save invalid paths?",
                f"These install folders did not validate: {names}.\n\nSave anyway?",
            ):
                return

        self.settings.set_active_game(active_game)
        self.settings.mod_prefix = self.mod_prefix.get().strip() or "B21"
        self.settings.setup_complete = True
        self.settings.save()
        env_path = export_settings_to_env(self.settings)

        self.completed = True
        messagebox.showinfo("Setup Complete", f"Saved setup and wrote {env_path}")
        self.root.destroy()
