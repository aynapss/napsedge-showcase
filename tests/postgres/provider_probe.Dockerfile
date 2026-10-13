# Phase 9B-7B: the probe-tooling image (TEST TOOLING; never an application image, never pushed).
# Built FROM the retained backup image, so the probe runs on exactly that image's Python, dependencies and PostgreSQL 16.15
# client. It adds ONLY the test-package pins the accepted disposable runner already uses, because the probe imports the
# accepted test-layer helpers. No source is copied: the repository is bind-mounted read-only at /repo at run time.
# Built with NO build context:
#   docker build --build-arg BACKUP_IMAGE=napsedge-backup:<tag> -t napsedge-probe-tooling:<tag> - < tests/postgres/provider_probe.Dockerfile
ARG BACKUP_IMAGE
FROM ${BACKUP_IMAGE}
USER root
RUN python -m pip install --no-cache-dir \
      pytest==9.1.1 pluggy==1.6.0 iniconfig==2.3.0 Pygments==2.19.2 \
      testcontainers==4.15.0 docker==7.2.0 wrapt==2.4.1 python-dotenv==1.2.1
USER napsedge
WORKDIR /repo
ENTRYPOINT ["python", "-m", "tests.postgres.provider_probe"]
