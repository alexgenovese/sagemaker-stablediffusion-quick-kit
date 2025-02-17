# -*- coding: utf-8 -*-
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

# Permission is hereby granted, free of charge, to any person obtaining a copy of
# this software and associated documentation files (the "Software"), to deal in
# the Software without restriction, including without limitation the rights to
# use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of
# the Software, and to permit persons to whom the Software is furnished to do so.

# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS
# FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR
# COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER
# IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN
# CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.

import json
import uuid
import os
import urllib
import boto3
from botocore.exceptions import ClientError
from fastapi import FastAPI, Request
from pydantic import BaseModel
from pprint import pprint
import base64
from fastapi.middleware.cors import CORSMiddleware


app = FastAPI()


app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


CURRENT_REGION= boto3.session.Session().region_name
SM_REGION=os.environ.get("SM_REGION") if os.environ.get("SM_REGION")!="" else CURRENT_REGION
SM_ENDPOINT=os.environ.get("SM_ENDPOINT",None) #SM_ENDPORT NAME
S3_BUCKET=os.environ.get("S3_BUCKET","")
S3_PREFIX=os.environ.get("S3_PREFIX","stablediffusion/asyncinvoke")
DDB_TABLE=os.environ.get("DDB_TABLE","") #dynamodb table name

print(f"CURRENT_REGION |{CURRENT_REGION}|")
print(f"SM_REGION |{SM_REGION}|")
print(f"S3_BUCKET |{S3_BUCKET}|")
print(f"S3_PREFIX |{S3_PREFIX}|")


GALLERY_ADMIN_TOKEN=os.environ.get("GALLERY_ADMIN_TOKEN","") #gallery admin token 

sagemaker_runtime = boto3.client("sagemaker-runtime", region_name=SM_REGION)
s3_client = boto3.client("s3")

class APIconfig:

    def __init__(self, item,include_attr=True):
        if include_attr:
            self.sm_endpoint = item.get('SM_ENDPOINT').get('S')
            self.label = item.get('LABEL').get('S'),
            self.hit = item.get('HIT',{}).get('S','')
        else:
            self.sm_endpoint = item.get('SM_ENDPOINT')
            self.label = item.get('LABEL') 
            self.hit = item.get('HIT','')


    def __repr__(self):
        return f"APIconfig<{self.label} -- {self.sm_endpoint}>"

# class APIConfigEncoder(JSONEncoder):
#         def default(self, o):
#             return o.__dict__

def get_s3_uri(bucket, prefix):
    """
    s3 url helper function
    """
    if prefix.startswith("/"):
        prefix=prefix.replace("/","",1)
    return f"s3://{bucket}/{prefix}"

def search_item(table_name, pk, prefix):
    #if env local_mock is true return local config
    dynamodb = boto3.client('dynamodb')
   
    if prefix == "":
        query_str = "PK = :pk "
        attributes_value={
        ":pk": {"S": pk},
        }
    else:
       query_str = "PK = :pk and begins_with(SM_ENDPOINT, :sk) "
       attributes_value[":sk"]={"S": prefix}
    
    resp = dynamodb.query(
        TableName=table_name,
        KeyConditionExpression=query_str,
        ExpressionAttributeValues=attributes_value,
        ScanIndexForward=True
    )
    items = resp.get('Items',[])
    return items

def async_inference(input_location,sm_endpoint=None):
    """"
    :param input_location: input_location used by sagemaker endpoint async
    :param sm_endpoint: stable diffusion model's sagemaker endpoint name
    """
    if sm_endpoint is None :
        raise Exception("Not found SageMaker")
    response = sagemaker_runtime.invoke_endpoint_async(
            EndpointName=sm_endpoint,
            InputLocation=input_location)
    return response["ResponseMetadata"]["HTTPStatusCode"], response.get("OutputLocation",'')

def get_async_inference_out_file(output_location):
    """
    :param output_locaiton: async inference s3 output location
    """
    s3_resource = boto3.resource('s3')
    output_url = urllib.parse.urlparse(output_location)
    bucket = output_url.netloc
    key = output_url.path[1:]
    try:
        obj_bytes = s3_resource.Object(bucket, key)
        value = obj_bytes.get()['Body'].read()
        data = json.loads(value)
        images=data['result']
        images=[x.replace(f"s3://{S3_BUCKET}",f"") for x in images]
        return {"status":"completed", "images":images}
    except ClientError as ex:
        if ex.response["Error"]["Code"] == "NoSuchKey":
            return {"status":"Pending"}
        else:
            return {"status":"Failed", "msg":"have other issue, please contact site admin"}



@app.get("/")
def read_root():
    return {"Hello": "World"}

@app.options("/")
def options():
    return {}


class Prompt(BaseModel):
    prompt: str
    negative_prompt: str | None = ""
    steps: int
    sampler: str
    seed: int
    height: int 
    width: int
    count: int
    image_url: str | None

@app.post("/async_hander")
def async_handler(prompt: Prompt, request: Request):
    #check request body
    body = prompt.json()
    pprint(json.loads(body))
    sm_endpoint = request.headers.get("x-sm-endpoint", None)
    print(sm_endpoint)
    #check sm_endpoint , if request have not check it from dynamodb
    if sm_endpoint is None:
        items=search_item(DDB_TABLE, "APIConfig", "")
        configs=[APIconfig(item) for item in items]
        if len(configs)>0:
            sm_endpoint=configs[0].sm_endpoint
        else:
             return {"msg":"not found SageMaker Endpoint"}
        
    input_file=str(uuid.uuid4())+".json"
    s3_resource = boto3.resource('s3')
    s3_object = s3_resource.Object(S3_BUCKET, f'{S3_PREFIX}/input/{input_file}')
    s3_object.put(
        Body=(bytes(body,encoding="UTF-8"))
    )
    print(f'input_location: s3://{S3_BUCKET}/{S3_PREFIX}/input/{input_file}')
    status_code, output_location=async_inference(f's3://{S3_BUCKET}/{S3_PREFIX}/input/{input_file}',sm_endpoint)
    status_code=200 if status_code==202 else 403
    return {"task_id":os.path.basename(output_location).split('.')[0]}

@app.get("/config")
def config():
    items=search_item(DDB_TABLE, "APIConfig", "")
    configs=[APIconfig(item) for item in items]
    # return result_json(200,configs,cls=APIConfigEncoder)
    return configs

@app.get("/task/{task_id}")
def task(task_id: str):
    result=get_async_inference_out_file(f"s3://{S3_BUCKET}/{S3_PREFIX}/out/{task_id}.out")
    status_code=200 if result.get("status")=="completed" else 204
    return result


class Token(BaseModel):
    token: str

@app.post('/auth')
def auth(token: Token):
    token = json.loads(token.json())['token']
    if token== GALLERY_ADMIN_TOKEN:
        return {'msg':"ok"}


class CustomFile(BaseModel):
    imageName: str
    imageData: str

@app.post('/upload_handler')
def upload(file: CustomFile):
    body = json.loads(file.json())
    
    file_content = base64.b64decode(body["imageData"])
    
    if "jpg" in body["imageName"] or "jpeg" in body["imageName"] :
        file_name=f"stablediffusion/upload/{str(uuid.uuid4())}.jpg"
        content_type="image/jpeg"
    else:
        file_name=f"stablediffusion/upload/{str(uuid.uuid4())}.png"
        content_type="image/png"
    # 保存文件到S3存储桶
    s3_client.put_object(
        Bucket=S3_BUCKET,
        Key=file_name,
        Body=file_content,
        ContentType=content_type
    )
    return {"upload_file": file_name}

