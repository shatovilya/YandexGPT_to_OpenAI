import asyncio
import json
import os
import time
from datetime import datetime
from typing import Optional, List, Union, Literal

import aiohttp
from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.security import OAuth2PasswordBearer
from dotenv import load_dotenv
from pydantic import BaseModel

from utils.misc import (
    chat_completion_chunk_translation,
    chat_completion_chunk_tool_translation,
    chat_completion_translation,
    embeddings_translation,
    get_model_list,
    image_generation_translation,
    messages_translation,
    setup_logging
)
from utils.tokens import get_tokens

load_dotenv()

logger = setup_logging(os.getenv('Y2O_LogFile', './logs/y2o.log'), os.getenv('Y2O_LogLevel', 'INFO').upper())

# Yandex API settings 
SECRETKEY = os.getenv('Y2O_SecretKey', None)
CATALOGID = os.getenv('Y2O_CatalogID', None)
# Is BYOK enabled (user can state their own key and catalog id in token as <CatalogID>:<SecretKey>)
BYOK = os.getenv('Y2O_BringYourOwnKey', 'False')
BYOK = BYOK.lower() in ['true', '1', 't', 'y', 'yes']

if SECRETKEY is None or CATALOGID is None:
    if not BYOK:
        logger.error("Y2O_SecretKey and Y2O_CatalogID must be set in .env file")
        raise Exception("Y2O_SecretKey and Y2O_CatalogID must be set in .env file")
    else:
        logger.warning("Y2O_SecretKey and Y2O_CatalogID not set, BYOK enabled")
else:
    logger.info(f"Yandex API settings: CatalogID: {CATALOGID}, SecretKey: ***{SECRETKEY[-4:]} (BYOK: {BYOK})")

# Get tokens
try:
    tokens = get_tokens()
except Exception as e:
    if not BYOK:
        raise e
    else:
        tokens = {}
        logger.warning("Tokens not loaded, BYOK enabled")

logger.info(f"Loaded tokens: {tokens}")
logger.info(f"Bring your own key: `{BYOK}` (let user state their own credentials as `<CatalogID>:<SecretKey>` instead of token)")

# Get model list
MODELS = get_model_list()

# Clear images folder
if os.path.exists("data/images"):
    logger.info("Images folder exists, deleting all images")
    for image in os.listdir("data/images"):
        if os.path.isfile(f"data/images/{image}") and image.endswith(".jpg"):
            os.remove(f"data/images/{image}")

print("=== YandexGPT to OpenAI API translator ===")
logger.info(f"=== YandexGPT to OpenAI API translator: Starting server (tokens: {len(tokens)}) ===")

# API settings
app = FastAPI(docs_url=None, redoc_url=None, title="YandexGPT to OpenAI API translator", description="Simple translator from OpenAI API calls to YandexGPT/YandexART API calls")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="token")

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=os.getenv('Y2O_CORS_Origins', '*').split(","),
    allow_credentials=True,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

async def authenticate_user(token: str = Depends(oauth2_scheme)):
    auth_response = {
        "user_id": None,
        "byok": None
    }
    # Return BYOK data if BYOK is enabled and token is in BYOK format
    if BYOK and ":" in token:
        catalogid, secretkey = token.split(":")
        logger.debug(f"BYOK credentials: ***{catalogid[-4:]}:***{secretkey[-4:]}")
        auth_response["byok"] = {
            "catalogid": catalogid,
            "secretkey": secretkey
        }
        return auth_response 
    # If BYOK is not enabled or token is not in BYOK format, check if token is in tokens list
    if len(tokens) > 0:
        if token in tokens:
            logger.debug(f"Valid token: {token}")
            user_id = tokens[token]
            auth_response["user_id"] = user_id
            return auth_response
    # At this point, token is not valid
    logger.warning(f"Invalid token: {token}")
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid token",
        headers={"WWW-Authenticate": "Bearer"},
    )

async def get_creds(auth: dict):
    catalogid = None
    secretkey = None
    user_id = None
    if auth["byok"] and BYOK:
        catalogid = auth["byok"]["catalogid"]
        secretkey = auth["byok"]["secretkey"]
    if auth["user_id"]:
        user_id = auth["user_id"]
        catalogid = CATALOGID
        secretkey = SECRETKEY
    return catalogid, secretkey, user_id

@app.middleware("http")
async def log_requests(request: Request, call_next):
    logger.debug(f"Incoming request: {request.method} {request.url}")
    if request.headers.get("Authorization"):
        logger.debug(f"Authorization Header: ...{request.headers['Authorization'][-4:]}")
    else:
        logger.debug("Authorization Header: Not provided")
    response = await call_next(request)
    logger.debug(f"Response status: {response.status_code}")
    return response

####################################
#           API endpoints          #
####################################

# Chat completions
class FunctionParameters(BaseModel):
    type: str
    properties: dict
    required: Optional[List[str]] = []

class FunctionDefinition(BaseModel):
    name: str
    description: Optional[str] = ""
    parameters: FunctionParameters

class Tool(BaseModel):
    type: Literal["function"]
    function: FunctionDefinition

class ChatCompletions(BaseModel):
    model: str
    max_tokens: Optional[int] = None
    temperature: float = 0.7
    messages: list
    stream: bool = False
    tools: Optional[List[Tool]] = None
    tool_choice: Optional[Union[str, dict]] = None

async def chat_model_alias(model: str):
    if model.startswith("gpt-3.5") or "mini" in model:
        model = "yandexgpt-lite/latest"
    elif model.startswith("gpt-4"):
        model = "yandexgpt/latest"
    else:
        pass
    return model

@app.post("/v1/chat/completions")
@app.post("/chat/completions")
async def chat_completions(chat_completions: ChatCompletions, auth: dict = Depends(authenticate_user)):
    logger.info(f"* User requested chat completion via model `{chat_completions.model}` (stream: {chat_completions.stream})")
    logger.debug(f"Request payload: {json.dumps(chat_completions.model_dump())}")
    if chat_completions.stream:
        return StreamingResponse(stream_chat_completions(chat_completions, auth), media_type="text/event-stream")
    else:
        return await non_stream_chat_completions(chat_completions, auth)

async def stream_chat_completions(chat_completions: ChatCompletions, auth: dict):
    catalogid, secretkey, user_id = await get_creds(auth)
    model = chat_completions.model
    model = await chat_model_alias(model)

    url = "https://llm.api.cloud.yandex.net/foundationModels/v1/completion"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Api-Key {secretkey}",
        "x-folder-id": catalogid,
        "x-data-logging-enabled": "false"
    }
    data = {
        "modelUri": f"gpt://{catalogid}/{model}",
        "completionOptions": {
            "stream": True,
            "temperature": chat_completions.temperature,
            "maxTokens": chat_completions.max_tokens
        },
        "messages": await messages_translation(chat_completions.messages)
    }

    if chat_completions.tools:
        data["tools"] = [tool.model_dump() for tool in chat_completions.tools]
    if chat_completions.tool_choice:
        data["tool_choice"] = chat_completions.tool_choice

    logger.debug(f"Request to YaGPT data: {json.dumps(data)}")

    async with aiohttp.ClientSession() as session:
        async with session.post(url, headers=headers, json=data) as response:
            if response.status != 200:
                logger.error(f"* User `{user_id}` received error: {response.status} - {await response.text()}")
                raise HTTPException(status_code=response.status, detail=await response.text())
            now = time.time()
            str_index = 0
            async for chunk in response.content.iter_any():
                if chunk:
                    chunk_text = chunk.decode('utf-8')
                    try:
                        data = json.loads(chunk_text)
                        message = data['result']['alternatives'][0]['message']

                        if 'text' in message:
                            # Handle standard text response
                            deltatext = message['text'][str_index:]
                            str_index = len(message['text'])
                            new_chunk = await chat_completion_chunk_translation(data, deltatext, user_id, model, timestamp=now)
                            yield f"data: {json.dumps(new_chunk)}\n\n"
                        
                        elif 'toolCallList' in message:
                            # Handle tool calls
                            tool_calls = message['toolCallList']['toolCalls']
                            for tool_call in tool_calls:
                                if 'functionCall' in tool_call:
                                    arguments = tool_call['functionCall'].get('arguments', {})
                                    if isinstance(arguments, dict):
                                        arguments = json.dumps(arguments)
                                        
                                    tool_function_call = {
                                        "id": f"call_{int(now)}_{hash(str(tool_call))}",
                                        "name": tool_call['functionCall']['name'],
                                        "arguments": arguments
                                    }
                                    
                                    new_chunk = await chat_completion_chunk_tool_translation(
                                        data, tool_function_call, user_id, model, timestamp=now)
                                    yield f"data: {json.dumps(new_chunk)}\n\n"
                    except json.JSONDecodeError:
                        # Skip incomplete JSON chunks
                        continue

async def non_stream_chat_completions(chat_completions: ChatCompletions, auth: dict):
    catalogid, secretkey, user_id = await get_creds(auth)
    model = chat_completions.model
    model = await chat_model_alias(model)

    url = "https://llm.api.cloud.yandex.net/foundationModels/v1/completion"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Api-Key {secretkey}",
        "x-folder-id": catalogid,
        "x-data-logging-enabled": "false"
    }
    data = {
        "modelUri": f"gpt://{catalogid}/{model}",
        "completionOptions": {
            "stream": False,
            "temperature": chat_completions.temperature,
            "maxTokens": chat_completions.max_tokens
        },
        "messages": await messages_translation(chat_completions.messages)
    }

    if chat_completions.tools:
        data["tools"] = [tool.model_dump() for tool in chat_completions.tools]
    if chat_completions.tool_choice:
        data["tool_choice"] = chat_completions.tool_choice

    logger.debug(f"Request to YaGPT data: {json.dumps(data)}")

    async with aiohttp.ClientSession() as session:
        async with session.post(url, headers=headers, json=data) as response:
            if response.status != 200:
                logger.error(f"* User `{user_id}` received error: {response.status} - {await response.text()}")
                raise HTTPException(status_code=response.status, detail=await response.text())
            response_data = await response.json()
            response_data = await chat_completion_translation(response_data, user_id, model)
            logger.info(f"* User `{user_id}` received chat completions (id: `{response_data['id']}`). Tokens used (prompt/completion/total): {response_data['usage']['prompt_tokens']}/{response_data['usage']['completion_tokens']}/{response_data['usage']['total_tokens']}")
            new_headers = {
                "Date": f"{datetime.now().strftime('%a, %d %b %Y %H:%M:%S GMT')}",
                "Content-Type": "application/json",
                "Connection": "keep-alive",
            }
            return JSONResponse(content=response_data, headers=new_headers)

# Embeddings
class Embeddings(BaseModel):
    model: str
    input: Union[str, List[str]]
    encoding_format: str = "float"

async def fetch_embeddings(url, headers, data):
    logger.debug(f"Fetching embeddings: {data}")
    async with aiohttp.ClientSession() as session:
        async with session.post(url, headers=headers, json=data) as response:
            if response.status != 200:
                logger.error(f"Received error: {response.status} - {await response.text()}")
                raise HTTPException(status_code=response.status, detail=await response.text())
            response_data = await response.json()
            logger.debug(f"Embeddings response: {response_data}")
            return response_data
        
async def embeddings_model_alias(model: str):
    if model in ["text-embedding-3-large"]:
        model = "text-search-doc/latest"
    elif model in ["text-embedding-3-small", "text-embedding-ada-002"]:
        model = "text-search-query/latest"
    else:
        pass
    return model

@app.post("/v1/embeddings")
@app.post("/embeddings")
async def embeddings(embeddings: Embeddings, auth: dict = Depends(authenticate_user)):
    catalogid, secretkey, user_id = await get_creds(auth)
    logger.info(f"* User `{user_id}` requested embeddings for model `{embeddings.model}`")
    model = embeddings.model
    model = await embeddings_model_alias(model)
    b64 = embeddings.encoding_format == "base64"

    url = "https://llm.api.cloud.yandex.net/foundationModels/v1/textEmbedding"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Api-Key {secretkey}",
        "x-folder-id": catalogid,
        "x-data-logging-enabled": "false"
    }

    # Ensure `embeddings.input` is always a list
    if isinstance(embeddings.input, str):
        embeddings.input = [embeddings.input]
    elif not isinstance(embeddings.input, list):
        logger.error(f"* User `{user_id}` received error: `input` must be a string or a list of strings")
        raise HTTPException(status_code=400, detail="`input` must be a string or a list of strings")

    results = []
    for i, text in enumerate(embeddings.input):
        if not isinstance(text, str):
            logger.error(f"* User `{user_id}` received error: `input` must be a string or a list of strings")
            raise HTTPException(status_code=400, detail="`input` must be a string or a list of strings")
        data = {
            "modelUri": f"emb://{catalogid}/{model}",
            "text": text
        }
        response_data = await fetch_embeddings(url, headers, data)
        results.append(response_data)

    response_data = await embeddings_translation(results, user_id, model=model, b64=b64)
    logger.info(f"* User `{user_id}` received embeddings for model `{model}`")
    logger.debug(f"Embeddings: {response_data}")
    return JSONResponse(content=response_data, media_type="application/json")
        
# Image generation
class ImageGeneration(BaseModel):
    model: str
    prompt: str
    n: int = 1
    size: str = "1024x1024"
    quality: str = "standard"
    response_format: str = "url"
    style: str = None
    timeout: int = 45

async def image_model_alias(model: str):
    if "dall-e" in model:
        model = "yandex-art/latest"
    return model

async def image_generation_request(secretkey: str, catalogid: str, model: str, prompt: str, size: str = "1024x1024"):
    """
    Request image generation from Yandex API
    https://yandex.cloud/ru/docs/foundation-models/image-generation/api-ref/ImageGenerationAsync/generate
    """
    size = size.split("x")
    if len(size) != 2:
        size = ["1024", "1024"]
    url = "https://llm.api.cloud.yandex.net/foundationModels/v1/imageGenerationAsync"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Api-Key {secretkey}",
        "x-folder-id": catalogid,
        "x-data-logging-enabled": "false"
    }
    data = {
        "modelUri": f"art://{catalogid}/{model}",
        "messages": [
            {
                "text": prompt,
                "weight": 1
            }
        ],
        "generationOptions": {
            "mimeType": "image/jpeg", # only `image/jpeg` supported
            # "seed": randint(0, 1000000),
            "aspectRatio": {
                "widthRatio": int(size[0]),
                "heightRatio": int(size[1])
            }
        }
    }

    async with aiohttp.ClientSession() as session:
        async with session.post(url, headers=headers, json=data) as response:
            if response.status != 200:
                logger.error(f"Received error: {response.status} - {await response.text()}")
                raise HTTPException(status_code=response.status, detail=await response.text())
            response_data = await response.json()
            if "error" in response_data:
                logger.error(f"Received error in response data: {response_data['error']}")
                raise HTTPException(status_code=500, detail=response_data['error'])
            operation_id = response_data["id"]
            return operation_id
        
async def image_generation_check(secretkey: str, catalogid: str, operation_id: str):
    url = f"https://llm.api.cloud.yandex.net:443/operations/{operation_id}"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Api-Key {secretkey}",
        "x-folder-id": catalogid,
        "x-data-logging-enabled": "false"
    }
    
    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=headers) as response:
            if response.status != 200:
                logger.error(f"Received error: {response.status} - {await response.text()}")
                raise HTTPException(status_code=response.status, detail=await response.text())
            response_data = await response.json()
            if "error" in response_data:
                raise HTTPException(status_code=500, detail=response_data['error']['message'])
            if response_data["done"]:
                if "response" in response_data:
                    return response_data
            else:
                return None

@app.post("/v1/images/generations")
@app.post("/images/generations")
async def image_generation(image_generation: ImageGeneration, auth: dict = Depends(authenticate_user)):
    catalogid, secretkey, user_id = await get_creds(auth)
    logger.info(f"* User `{user_id}` requested image generation via model `{image_generation.model}`")
    model = image_generation.model
    model = await image_model_alias(model)
    b64 = image_generation.response_format == "b64_json"
    timeout = image_generation.timeout

    # request image generation
    created_at = int(time.time())
    operation_id = await image_generation_request(secretkey, catalogid, model, image_generation.prompt, image_generation.size)
    response_data = None
    # check image generation status
    i = 0
    while response_data is None:
        response_data = await image_generation_check(secretkey, catalogid, operation_id)
        i += 1
        if i > timeout:
            logger.error(f"* User `{user_id}` image generation timeout")
            raise HTTPException(status_code=500, detail=f"Image generation timeout ({timeout}s)")
        await asyncio.sleep(1)

    response_data = await image_generation_translation(response_data, user_id, created_at, b64)
    if not b64:
        static_images_url = f"{os.getenv('Y2O_ServerURL', 'http://127.0.0.1:8520')}/images"
        response_data["data"][0]["url"] = f'{static_images_url}/{response_data["data"][0]["url"]}'
    logger.info(f"* User `{user_id}` received image generation (id: `{operation_id}`).")
    return JSONResponse(content=response_data, media_type="application/json")

@app.get("/v1/images/{image_id}")
@app.get("/images/{image_id}")
async def get_image(image_id: str, auth: dict = Depends(authenticate_user)):
    catalogid, secretkey, user_id = await get_creds(auth)
    logger.info(f"* User `{user_id}` requested generated image `{image_id}`")
    if not os.path.exists(f"data/images/{image_id}"):
        logger.error(f"* User `{user_id}` requested image `{image_id}` not found")
        raise HTTPException(status_code=404, detail="Image not found")
    return StreamingResponse(open(f"data/images/{image_id}", "rb"), media_type="image/jpeg")

# Models
@app.get("/v1/models")
@app.get("/models")
async def models_list(auth: dict = Depends(authenticate_user)):
    catalogid, secretkey, user_id = await get_creds(auth)
    logger.info(f"* User `{user_id}` requested models list")
    models = {
        "object": "list",
        "data": MODELS,
        "object": "list"
        }
    logger.info(f"* User received models list")
    return JSONResponse(content=models, media_type="application/json")

# Health check
@app.get("/v1/health")
@app.get("/health")
async def health_check():
    return {"status": "ok"}



####################################
#               Start              #
####################################
if __name__ == "__main__":
    import uvicorn
    if os.getenv('Y2O_SSL_Key') and os.getenv('Y2O_SSL_Cert'):
        logger.info("SSL keys found, starting server with SSL")
        uvicorn.run(app, host=os.getenv('Y2O_Host', '0.0.0.0'), port=int(os.getenv('Y2O_Port', 8520)), ssl_keyfile=os.getenv('Y2O_SSL_Key'), ssl_certfile=os.getenv('Y2O_SSL_Cert'))
    else:
        logger.info("Starting server without SSL")
        uvicorn.run(app, host=os.getenv('Y2O_Host', '0.0.0.0'), port=int(os.getenv('Y2O_Port', 8520)))
