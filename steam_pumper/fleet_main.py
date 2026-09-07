from .application import run_application


TOPOLOGY = "fleet"


if __name__ == "__main__":
    raise SystemExit(run_application(TOPOLOGY))
