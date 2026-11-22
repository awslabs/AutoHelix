# The image of an arm's container (arms/container.sh runs it): Amazon Linux 2023, as on the
# host, so the host-built venvs and Python, mounted at their host paths, run unchanged; plus
# the root-owned GPU entry point (ar-gpu) and the container's command (entrypoint.sh).
#
#   docker build --build-arg AGENT_UID=$(id -u) -t ar-arm arms/container
FROM public.ecr.aws/amazonlinux/amazonlinux:2023
ARG AGENT_UID=1000

# gcc: Triton compiles a small helper against libcuda when training starts
RUN dnf install -y --setopt=install_weak_deps=False \
        git sudo util-linux procps-ng gawk tar gzip findutils which less diffutils gcc \
    && dnf clean all && rm -rf /var/cache/dnf /var/log/dnf*

# agent: the session (and AutoHelix's harness), with the host operator's uid (AGENT_UID) so
# the project stays theirs. ar: training and scoring, the only user besides root that may
# open the GPUs (entrypoint.sh). /srv/ar (root only): run.env, the base commit, the run's
# state. /srv/ar-data (root and ar): the data.
RUN useradd -u "$AGENT_UID" -U -M -d /home/agent agent \
    && useradd -u 2000 -U -M -d /nonexistent -s /sbin/nologin ar \
    && install -d -m 0700 /srv/ar \
    && install -d -m 0750 -g ar /srv/ar-data \
    && git config --system user.name autoresearch \
    && git config --system user.email autoresearch@localhost \
    && git config --system --add safe.directory /srv/ar/base.git

COPY ar-gpu entrypoint.sh /usr/local/sbin/
COPY run_experiment /usr/local/bin/
COPY sudoers /etc/sudoers.d/ar
RUN chmod 0755 /usr/local/sbin/ar-gpu /usr/local/sbin/entrypoint.sh /usr/local/bin/run_experiment \
    && chmod 0440 /etc/sudoers.d/ar && visudo -c

ENTRYPOINT ["/usr/local/sbin/entrypoint.sh"]
