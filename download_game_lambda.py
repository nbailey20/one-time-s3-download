import boto3
from botocore.exceptions import ClientError
from botocore.client import Config
import os, json, logging, sys



INVALID_DOWNLOAD_CODE = {
    "statusCode": 403,
    "body": "Invalid download code."
}

EXPIRED_DOWNLOAD_CODE = {
    "statusCode": 403,
    "body": "Expired download code."
}

UNKNOWN_ERROR = {
    "statusCode": 404,
    "body": "Unexpected request."
}

SUCCESS_CODE_ADD = {
    "statusCode": 200,
    "body": "New code added."
}

INVALID_CODE_ADD = {
    "statusCode": 403,
    "body": "Code is either expired or already active."
}


## Wrapper around AWS library methods to read/write to/from file in storage using provided API client
## If no codebank exists (first execution), create new one
## Returns codebank if action == read, otherwise writes provided codebank_data to storage if action == write
def read_write_codebank(action, s3_client, codebank_name, bucket, codebank_data=None):
    if action == 'read':
        try:
            res = s3_client.get_object(Bucket=bucket, Key=codebank_name)
            file_content = res['Body'].read().decode('utf-8')
            logging.info('Successfully retrieved codebank from storage')
        except ClientError as e:
            if e.response['Error']['Code'] == 'NoSuchKey':
                logging.info('First-time setup detected, creating empty codebank')
                file_content = '{"unused_codes": [], "expired_codes": []}'
            else:
                logging.error(f'Could not retrieve codebank, exiting: {e}')
                sys.exit(3)
            
        try:
            codebank = json.loads(file_content)
            logging.info('Successfully parsed codebank')
            return codebank

        except:
            logging.error('Could not read codebank, exiting')
            sys.exit(3)

    elif action == 'write':
        try:
            s3_client.put_object(
                Bucket=bucket, 
                Key=codebank_name, 
                Body=(bytes(json.dumps(codebank_data).encode('utf-8')))
            )
            logging.info('Successfully wrote updated codebank to storage')
            return

        except ClientError as e:
            logging.error(f'Could not write codebank to storage, exiting: {e}')
            sys.exit(3)




## Adds new code to codebank and saves to storage
## Returns False if code already known, True if successfully added
def add_new_code(s3_client, codebank_name, bucket, codebank_data, path):
    try:
        new_code = path.split('/add_code=')[1]
    except:
        logging.error('Could not parse code to add from URL query, exiting')
        return UNKNOWN_ERROR

    if new_code in codebank_data['expired_codes'] or new_code in codebank_data['unused_codes']:
        logging.info(f'Requestor tried to add previously seen code {new_code}, aborting add')
        return INVALID_CODE_ADD

    codebank_data['unused_codes'].append(new_code)
    logging.info(f'Successfully added new code {new_code} to codebank')
    read_write_codebank('write', s3_client, codebank_name, bucket, codebank_data=codebank_data)
    return SUCCESS_CODE_ADD



## Changes codebank code from unused to expired, 
## Returns False if code already expired, otherwise updated codebank
def expire_used_code(codebank_data, code):
    if code in codebank_data['expired_codes']:
        logging.info(f'Code {code} is already expired, aborting')
        return False

    codebank_data['expired_codes'].append(code)
    codebank_data['unused_codes'].remove(code)
    logging.info(f'Changing code {code} to expired')
    return codebank_data



## Checks provided code against unused_codes in codebank and sets value to expired if match
## Returns True if valid code was provided and marked expired in codebank
## Returns INVALID/EXPIRED Download Code HTTP response if not valid code
def activate_code(input_code, s3_client, codebank_name, bucket, codebank_data):
    for valid_code in codebank_data['unused_codes']:
        if input_code == valid_code:
            updated_codebank = expire_used_code(codebank_data, valid_code)

            if not updated_codebank:
                return EXPIRED_DOWNLOAD_CODE

            read_write_codebank('write', s3_client, codebank_name, bucket, codebank_data=updated_codebank)
            return True

    return INVALID_DOWNLOAD_CODE


## Parses input code from URL path, returns HTTP 302 response to S3 presigned URL if valid
## Returns 403 response if invalid/expired code
def download_game_file(s3_client, codebank_name, bucket, codebank_data, path, game_file):
    try:
        input_code = path[1:] ## strip leading /
    except:
        logging.error('Could not parse provided code from URL, exiting')
        return UNKNOWN_ERROR

    ## Validate / expire input code
    activate_response = activate_code(input_code, s3_client, codebank_name, bucket, codebank_data)

    if activate_response == INVALID_DOWNLOAD_CODE or activate_response == EXPIRED_DOWNLOAD_CODE:
        return activate_response

    ## Generate URL to download game file
    ## Expire URL in 5 seconds so it can't be reused/shared
    try:
        presigned_url = s3_client.generate_presigned_url(
            'get_object', 
            Params={'Bucket': bucket, 'Key': game_file},
            ExpiresIn=5
        )
    except ClientError as e:
        logging.error(f'Could not generate URL for game file even though valid code was provided, exiting: {e}')
        sys.exit(4)

    
    ## Redirect validated requestor to temporary game file URL
    validated_redirect = {
        "statusCode": 302,
        "headers": {
            "Location": presigned_url,
        }
    }
    return validated_redirect





## Example API usage: <apidomain>.amazonaws.com/12345 for downloading gamefile with code '12345'
##   <apidomain>.amazonaws.com/add_code=67890 for adding new code '67890' to codebank
##
## Codebank format: {'unused_codes': [], 'expired_codes': []}
def lambda_handler(event, _):
    logging.getLogger().setLevel(logging.INFO)


    ## Get environment variables needed for script
    try:
        access_key_id = os.environ['access_key_id']
        secret_access_key = os.environ['secret_access_key']
        bucket = os.environ['download_bucket']
        file_name = os.environ['game_file_name']
        codebank_name = 'codebank.json'
        region = os.environ['region']
    except:
        logging.error('Could not retrieve environment variables needed for script, exiting')
        sys.exit(1)
    

    ## Create AWS storage API client with Lambda user creds to interact with codebank/game files
    try:
        lambda_user_session = boto3.Session(aws_access_key_id=access_key_id, 
                                            aws_secret_access_key=secret_access_key, 
                                            region_name=region
                                            )
        s3_client = lambda_user_session.client('s3', config=Config(signature_version='s3v4'))
    except ClientError as e:
        logging.error(f'Could not create API client for S3 using provided user creds, exiting: {e}')
        sys.exit(2)


    ## Read codebank data from storage
    codebank = read_write_codebank('read', s3_client, codebank_name, bucket)


    path = event['path']

    ## Add request received 
    if path.startswith('/add_code='):
        return add_new_code(s3_client, codebank_name, bucket, codebank, path)

    ## Download request received
    elif not path.startswith('/add_code='):
        http_response = download_game_file(s3_client, codebank_name, bucket, codebank, path, file_name)
        logging.info(f'Sending HTTP response {http_response}, script complete.')
        return http_response