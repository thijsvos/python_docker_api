import asyncio
import aiofiles
import os.path
import platform
import tarfile
import json
from docker import DockerClient
from docker.errors import NotFound
from fastapi import FastAPI, Body, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.security import HTTPBasicCredentials, HTTPBasic
from starlette.status import HTTP_401_UNAUTHORIZED
from typing import Dict, List, Union
from io import BytesIO


# Start with: $ uvicorn main:app --workers 4 --reload
# TODO: Look at the --workers parameter for uvicorn, because it does not work with the current setup.
#       It will say that the container is running while it is exited.
#       For now put it as --workers 1, or leave it out.

app = FastAPI()
security = HTTPBasic()

# To enable a frontend to make requests to this api, set CORS shit
# https://stackoverflow.com/questions/65635346/how-can-i-enable-cors-in-fastapi
origins = ["*"]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

client = DockerClient()
# Check which platform the API is running on, to pull and run the right container architecture.
match platform.processor():
    case "arm":
        platform = "arm64"
    case "x86_64":
        platform = "x86_64"
    case _:
        platform = "x86_64"

# TODO [POSSIBLY]: store the container state in a database instead of memory.
container_state = {}


async def read_credentials(secrets_file_location: str):
    """
    Read the username and password credentials from the specified secrets file.

    Parameters:
     - secrets_file_location (str): The location of the secrets file.

    Returns:
     - Tuple[str, str]: A tuple containing the username and password.
    """
    async with aiofiles.open(secrets_file_location, mode="r") as file:

        content = await file.read()

        try:
            config = json.loads(content)
            username = config["server_username"]
            password = config["server_password"]
        except (json.JSONDecodeError, KeyError):
            raise ValueError("The secrets file is invalid or does not contain a username or password.")

        return username, password


async def authenticate(credentials: HTTPBasicCredentials):
    """
    Authenticate the provided credentials against the stored username and password.

    Parameters:
     - credentials (HTTPBasicCredentials): The credentials provided for authentication.
    """
    username, password = await read_credentials(secrets_file_location="secrets.json")
    # Check if the provided username and password are valid
    if credentials.username != username or credentials.password != password:
        raise HTTPException(
            status_code=HTTP_401_UNAUTHORIZED,
            detail="Invalid username or password",
            headers={"WWW-Authenticate": "Basic"},
        )
    return True


@app.get("/")
async def root():
    return "up"


@app.get("/protected")
async def protected_route(credentials: HTTPBasicCredentials = Depends(security)):
    """
    Test endpoint to check if auth works.

    Parameters:
     - credentials: Admin creds to the api.
    """
    if await authenticate(credentials):
        return JSONResponse(content={"message": "Authenticated & Authorized."}, status_code=200)
    else:
        return JSONResponse(content={"message": "Unauthorized"}, status_code=HTTP_401_UNAUTHORIZED)


@app.get("/images")
async def list_images(credentials: HTTPBasicCredentials = Depends(security)) -> Union[List[str], Dict[str, str]]:
    """
    Retrieves a list of Docker images available on the host.

    Returns:
     - list: A list of Docker image tags as strings.
    """
    try:
        if await authenticate(credentials):
            # Get the list of images from the Docker client
            images = client.images.list()

            # Extract the tags from the images
            image_tags = [img.tags[0] for img in images if img.tags]

            return image_tags

    except HTTPException:
        return JSONResponse(content={"message": "Unauthorized"}, status_code=HTTP_401_UNAUTHORIZED)

    except Exception as e:
        return JSONResponse(content={"message": str(e)}, status_code=500)


@app.post("/images/pull")
async def pull_image(image: str = Body(...), 
                     credentials: HTTPBasicCredentials = Depends(security)):
    """
    Pulls a Docker image from Docker Hub.

    Parameters:
     - image (str): Docker image to pull.

    Returns:
     - dict: A dictionary with a message indicating whether the image was pulled successfully or not as a JSON object.
    """

    if not image:
        return {"message": "Docker image must be provided."}

    try:
        if await authenticate(credentials):
            # Pull the image
            pulled_image = client.images.pull(repository=image, platform=platform)

            return {"message": f"Image {pulled_image.tags[0]} pulled successfully."}
    
    except HTTPException:
        return JSONResponse(content={"message": "Unauthorized"}, status_code=HTTP_401_UNAUTHORIZED)

    except Exception as e:
        return JSONResponse(content={"message": str(e)}, status_code=500)


@app.post("/containers/create")
async def create_container(name: str = Body(...),
                           image: str = Body(...),
                           command: str = Body(...),
                           volumes: Dict[str, Dict[str, str]] = Body({}),
                           credentials: HTTPBasicCredentials = Depends(security)):
    """
    Creates a new Docker container with the given name, image, and command.

    Parameters:
    name (str): Name of the new container.
    image (str): Docker image to use for the container.
    command (str): Command to run in the container.
    volumes (Dict[str, Dict[str, str]]): A dictionary of volumes to mount in the container,
    where the key is the host path, and the value is a dictionary containing the container path and mount options.

    Returns:
    dict: A dictionary with a message indicating whether the container was created successfully or not as a JSON object.
    """
    if not name:
        return {"message": "Container name must be provided."}

    if not image:
        return {"message": "Docker image must be provided."}

    if not command:
        return {"message": "Command to run in the container must be provided."}

    try:
        if await authenticate(credentials):
            # Start the container in detached mode, dependent on the platform it is running on (ARM or x86).
            container = client.containers.run(image=image,
                                            command=command,
                                            name=name,
                                            detach=True,
                                            volumes=volumes,
                                            platform=platform)

            # Define a coroutine to monitor the container status
            async def _monitor_container():
                """
                Coroutine to monitor the status of a Docker container.

                Returns:
                None

                Example:
                To monitor the status of a container, run the following:

                ```
                # Define the name of the container to monitor
                container_name = "my-container"

                # Create the Docker container
                create_container(name=container_name, image="ubuntu:latest", command="sleep 60")

                # Start monitoring the container status
                asyncio.create_task(monitor_container())

                # Wait for the container to exit
                while container_state[container_name]["status"] != "exited":
                    await asyncio.sleep(1)

                # Print the container exit code
                print(f"Container {container_name} exited with code {container_state[container_name]['exit_code']}")
                ```
                """
                while True:
                    # Check the container status
                    container.reload()
                    state = {
                        "name": container.name,
                        "status": container.status,
                        "exit_code": container.attrs["State"]["ExitCode"]
                    }

                    # Update the container state dictionary
                    container_state[name] = state

                    if state["status"] == "exited":
                        # TODO [POSSIBLY]: Write logs to database.
                        break

                    # Wait for 1 second before checking again
                    await asyncio.sleep(1)

            # Start the coroutine to monitor the container status
            asyncio.create_task(_monitor_container())

        return {"message": f"Container {name} created successfully."}

    except HTTPException:
        return JSONResponse(content={"message": "Unauthorized"}, status_code=HTTP_401_UNAUTHORIZED)
    
    except Exception as e:
        return JSONResponse(content={"message": str(e)}, status_code=500)


@app.get("/containers/{name}/state")
async def get_container_state(name: str,
                              credentials: HTTPBasicCredentials = Depends(security)):
    """
    Gets the state of the specified Docker container.

    Parameters:
    name (str): Name of the Docker container to get the state of.

    Returns:
    dict: A dictionary with the state of the specified Docker container or
    a message indicating that the container was not found.

    Example:
    curl http://localhost:8000/containers/my-container/state
    """
    if await authenticate(credentials):
        if name not in container_state:
            return {"message": f"Container {name} not found."}
        else:
            return container_state[name]


@app.get("/containers/{name}/logs")
async def get_container_logs(name: str,
                             credentials: HTTPBasicCredentials = Depends(security)):
    """
    Returns the logs for the specified Docker container.

    Parameters:
    name (str): Name of the Docker container to get the logs of.

    Returns:
    dict: A dictionary with the logs of the specified Docker container or
    a message indicating that the container was not found.

    Example:
    curl http://localhost:8000/containers/my-container/logs
    """
    try:
        if await authenticate(credentials):
            container = client.containers.get(name)

    except HTTPException:
        return JSONResponse(content={"message": "Unauthorized"}, status_code=HTTP_401_UNAUTHORIZED)
    except NotFound:
        return {"message": f"Container {name} not found."}
    except Exception as e:
        return JSONResponse(content={"message": str(e)}, status_code=500)

    logs = container.logs()
    return {"logs": logs}


def save_response_to_file(response, file_name: str):
    """
    Save the content of a response object to a file.

    Parameters:
    - response (Iterable): An iterable response object with binary content.
    - file_name (str): The name of the file to save the content to.
    """
    with open(file_name, 'wb') as f:
        for chunk in response:
            f.write(chunk)


def read_file_content_from_tar(file_name: str):
    """
    Extract the content of the first file found in a tar archive.

    Parameters:
    - file_name (str): The name of the tar file to read the content from.

    Returns:
    - file_content (bytes): The content of the first file found in the tar archive, or None if no file is found.
    """
    with open(file_name, 'rb') as infile:
        content = infile.read()

    tar = tarfile.open(fileobj=BytesIO(content), mode='r')

    file_content = None
    for member in tar.getmembers():
        if member.isfile():
            file_content = tar.extractfile(member).read()
    return file_content


def save_file_content(file_content, file_name: str):
    """
    Save binary file content to a file.

    Parameters:
    - file_content (bytes): The binary content to be saved.
    - file_name (str): The name of the file to save the content to.
    """
    with open(file_name, 'wb') as outfile:
        outfile.write(file_content)


@app.post("/containers/{container}/download")
async def download_files(container: str,
                         path_to_file_in_container: str = Body(...),
                         host_directory: str = Body(...),
                         credentials: HTTPBasicCredentials = Depends(security)):
    """
    Download a file from a Docker container.

    Parameters:
    - container (str): The name or ID of the Docker container.
    - path_to_file_in_container (str): The path to the file to download, relative to the container's root.
    - host_directory (str): The path to the directory on the host where the file will be saved.

    Returns:
    - dict: A dictionary with a single key "message" and a string value indicating the result of the operation.

    Usage:
    container_name = "subfinder"
    file_paths = ["/root/.config/subfinder/config.yaml", "/root/.config/subfinder/provider-config.yaml"]
    host_path = "/home/user/test"
    tasks = [asyncio.create_task(download_file_from_container(container_name, file_path, host_path))
             for file_path in file_paths]
    downloaded_files = await asyncio.gather(*tasks)
    """
    try:
        if await authenticate(credentials):
            _container = client.containers.get(container)
            response, stat = _container.get_archive(path=path_to_file_in_container)
            file_name = os.path.basename(path_to_file_in_container)

            host_path_and_filename = f"{host_directory}/{file_name}"
            save_response_to_file(response, host_path_and_filename)
            file_content = read_file_content_from_tar(host_path_and_filename)
            save_file_content(file_content, host_path_and_filename)
            return {"message": f"File '{host_path_and_filename}' successfully downloaded."}
    
    except HTTPException:
        return JSONResponse(content={"message": "Unauthorized"}, status_code=HTTP_401_UNAUTHORIZED)
    except Exception as e:
        return JSONResponse(content={"message": str(e)}, status_code=500)


@app.post("/containers/stop")
# The 'embed=True' below is there because the body has 1 value ('name'), see the link below for explanation.
# https://github.com/tiangolo/fastapi/issues/1097
async def stop_container(name: str = Body(..., embed=True),
                         credentials: HTTPBasicCredentials = Depends(security)):
    """
    Stops the Docker container with the given name.

    Parameters:
    - name (str): Name of the Docker container to stop.

    Returns:
    - dict: A dictionary with a message indicating that the container was stopped successfully or
            a message indicating that the container was not found.
    """
    if not name:
        return {"message": "Container name must be provided."}
    elif container_state[name]["status"] == "exited":
        return {"message": f"Container {name} has already stopped."}

    try:
        if await authenticate(credentials):
            container = client.containers.get(name)

    except NotFound:
        return {"message": f"Container {name} not found."}
    except HTTPException:
        return JSONResponse(content={"message": "Unauthorized"}, status_code=HTTP_401_UNAUTHORIZED)
    except Exception as e:
        return JSONResponse(content={"message": str(e)}, status_code=500)

    container.stop()
    return {"message": f"Container {name} stopped successfully."}


@app.post("/containers/delete")
async def delete_container(name: str = Body(..., embed=True),
                           credentials: HTTPBasicCredentials = Depends(security)):
    """
    Deletes the Docker container with the given name.

    Parameters:
    - name (str): Name of the Docker container to delete.

    Returns:
    - dict: A dictionary with a message indicating that the container was deleted successfully or
            a message indicating that the container was not found.
    """
    if not name:
        return {"message": "Container name must be provided."}
    try:
        if await authenticate(credentials):
            container = client.containers.get(name)
    
    except HTTPException:
        return JSONResponse(content={"message": "Unauthorized"}, status_code=HTTP_401_UNAUTHORIZED)
    except NotFound:
        return {"message": f"Container {name} not found."}
    except Exception as e:
        return JSONResponse(content={"message": str(e)}, status_code=500)

    container.remove()
    return {"message": f"Container {name} deleted successfully."}
