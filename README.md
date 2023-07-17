# Python Docker API

This repository contains the code for a FastAPI-based API server. The server provides endpoints to interact with Docker containers, including listing available images, pulling images from Docker Hub, creating and managing containers, retrieving container state and logs, and downloading files from containers.

## Prerequisites

Have Docker (daemon) installed and running on the machine you install the API on.
Install the latest version of Python.

## Installation

Clone this repository.
Install python packages using the requirements.txt file: `pip install -r requirements.txt`.

## Usage

Obviously change the password in the `secrets.json` file.

run the API using the following command: `$ uvicorn main:app`.

Then view the API documentation through the following url: `http://127.0.0.1:8000/docs`.
