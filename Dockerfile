FROM debian:jessie


ENV DEBIAN_FRONTEND noninteractive 

# Install debian packages all at once
RUN  apt-get update && apt-get install -yqq --no-install-recommends \
		ca-certificates \
		python-pip \
	&& rm -rf /var/lib/apt/lists/*

RUN pip install awscli boto3 pyyaml pytz slackweb

ADD docker-entrypoint.sh /
ADD ecr-purger opt/ecr-purger
RUN chmod +x /docker-entrypoint.sh /opt/ecr-purger/ecr-purger.py

CMD ["start"]
ENTRYPOINT ["/docker-entrypoint.sh"]
