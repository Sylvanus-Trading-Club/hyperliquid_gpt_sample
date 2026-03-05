import logging


def setup_logging(level: str = "INFO", to_file: bool = True, file_path: str = "bot.log") -> logging.Logger:
    logger = logging.getLogger("hlbot")
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    logger.handlers.clear()

    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")

    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    logger.addHandler(sh)

    if to_file:
        fh = logging.FileHandler(file_path)
        fh.setFormatter(fmt)
        logger.addHandler(fh)

    return logger