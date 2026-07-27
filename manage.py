#!/usr/bin/env python
"""Point d'entree des commandes d'administration Django."""

import os
import sys


def main() -> None:
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.dev")
    try:
        from django.core.management import execute_from_command_line
    except ImportError as exc:  # pragma: no cover
        raise ImportError(
            "Django est introuvable. L'environnement virtuel est-il active ?"
        ) from exc
    execute_from_command_line(sys.argv)


if __name__ == "__main__":
    main()
