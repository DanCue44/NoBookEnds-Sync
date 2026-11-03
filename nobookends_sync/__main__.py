"""Run the service: python -m nobookends_sync"""

import logging
import os


def main():
    import uvicorn

    from .api import create_app

    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    uvicorn.run(
        create_app(),
        host=os.environ.get("HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT", "8000")),
        proxy_headers=True,
        forwarded_allow_ips="*",
        access_log=os.environ.get("ACCESS_LOG", "false").lower() == "true",
    )


if __name__ == "__main__":
    main()
