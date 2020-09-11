FROM python:3.6

# docker packages for docker-in-docker scheduler

RUN apt-get update && \
  apt-get -y install lsb-release software-properties-common apt-transport-https && \
  add-apt-repository \
  "deb [arch=amd64] https://download.docker.com/linux/debian \
  $(lsb_release -cs) \
  stable" && \
  apt-get update && \
  apt-get -y --allow-unauthenticated install docker-ce docker-ce-cli containerd.io && \
  export CLOUD_SDK_REPO="cloud-sdk-$(lsb_release -c -s)" && \
  echo "deb http://packages.cloud.google.com/apt $CLOUD_SDK_REPO main" | tee -a /etc/apt/sources.list.d/google-cloud-sdk.list && \
  curl https://packages.cloud.google.com/apt/doc/apt-key.gpg | apt-key add - && \
  apt-get update -y && apt-get install google-cloud-sdk -y && \
  apt-get -y install kubectl


COPY requirements.txt /app/requirements.txt
RUN pip install --upgrade -r /app/requirements.txt

COPY ./ /app
WORKDIR /app

CMD python scheduler/service.py
