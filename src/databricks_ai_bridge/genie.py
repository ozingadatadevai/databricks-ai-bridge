import bisect
import logging
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Union
import json

import mlflow
import pandas as pd
import tiktoken
from databricks.sdk import WorkspaceClient

MAX_TOKENS_OF_DATA = 20000
MAX_ITERATIONS = 50


# Define a function to count tokens
def _count_tokens(text):
    encoding = tiktoken.encoding_for_model("gpt-4o")
    return len(encoding.encode(text))

def _to_json_string(data: pd.DataFrame) -> str:
    json_data = data.to_json(orient="records")
    return json.dumps(json_data)

@dataclass
class GenieResponse:
    result: Union[str, pd.DataFrame]
    query: Optional[str] = ""
    description: Optional[str] = ""
    conversation_id: Optional[str] = None


@mlflow.trace(span_type="PARSER")
def _parse_query_result(resp) -> Union[str, pd.DataFrame]:
    output = resp["result"]
    if not output:
        return "EMPTY"

    columns = resp["manifest"]["schema"]["columns"]
    header = [str(col["name"]) for col in columns]
    rows = []

    for item in output["data_array"]:
        row = []
        for column, value in zip(columns, item):
            type_name = column["type_name"]
            if value is None:
                row.append(None)
                continue

            if type_name in ["INT", "LONG", "SHORT", "BYTE"]:
                row.append(int(value))
            elif type_name in ["FLOAT", "DOUBLE", "DECIMAL"]:
                row.append(float(value))
            elif type_name == "BOOLEAN":
                row.append(value.lower() == "true")
            elif type_name == "DATE" or type_name == "TIMESTAMP":
                row.append(datetime.strptime(value[:10], "%Y-%m-%d").date())
            elif type_name == "BINARY":
                row.append(bytes(value, "utf-8"))
            else:
                row.append(value)

        rows.append(row)

    dataframe = pd.DataFrame(rows, columns=header)
    query_result = dataframe.to_markdown()
    tokens_used = _count_tokens(query_result)

    # If the full result fits, return it
    if tokens_used <= MAX_TOKENS_OF_DATA:
        return query_result.strip()

    def is_too_big(n):
        return _count_tokens(dataframe.iloc[:n].to_markdown()) > MAX_TOKENS_OF_DATA

    # Use bisect_left to find the cutoff point of rows within the max token data limit in a O(log n) complexity
    # Passing True, as this is the target value we are looking for when _is_too_big returns
    cutoff = bisect.bisect_left(range(len(dataframe) + 1), True, key=is_too_big)

    # Slice to the found limit
    truncated_df = dataframe.iloc[:cutoff]

    # Edge case: Cannot return any rows because of tokens so return an empty string
    if len(truncated_df) == 0:
        return ""

    truncated_result = truncated_df.to_markdown()

    # Double-check edge case if we overshot by one
    if _count_tokens(truncated_result) > MAX_TOKENS_OF_DATA:
        truncated_result = truncated_df.iloc[:-1].to_markdown()

    return truncated_result.strip()

@mlflow.trace(span_type="PARSER")
def _parse_query_result_json(resp) -> Union[str, pd.DataFrame]:
    output = resp["result"]
    if not output:
        return "EMPTY"

    columns = resp["manifest"]["schema"]["columns"]
    header = [str(col["name"]) for col in columns]
    rows = []

    for item in output["data_array"]:
        row = []
        for column, value in zip(columns, item):
            type_name = column["type_name"]
            if value is None:
                row.append(None)
                continue

            if type_name in ["INT", "LONG", "SHORT", "BYTE"]:
                row.append(int(value))
            elif type_name in ["FLOAT", "DOUBLE", "DECIMAL"]:
                row.append(float(value))
            elif type_name == "BOOLEAN":
                row.append(value.lower() == "true")
            elif type_name == "DATE" or type_name == "TIMESTAMP":
                row.append(datetime.strptime(value[:10], "%Y-%m-%d").date())
            elif type_name == "BINARY":
                row.append(bytes(value, "utf-8"))
            else:
                row.append(value)

        rows.append(row)

    dataframe = pd.DataFrame(rows, columns=header)
    query_result = _to_json_string(dataframe)
    tokens_used = _count_tokens(query_result)

    # If the full result fits, return it
    if tokens_used <= MAX_TOKENS_OF_DATA:
        return query_result.strip()

    def is_too_big(n):
        return _count_tokens(_to_json_string(dataframe.iloc[:n])) > MAX_TOKENS_OF_DATA

    # Use bisect_left to find the cutoff point of rows within the max token data limit in a O(log n) complexity
    # Passing True, as this is the target value we are looking for when _is_too_big returns
    cutoff = bisect.bisect_left(range(len(dataframe) + 1), True, key=is_too_big)

    # Slice to the found limit
    truncated_df = dataframe.iloc[:cutoff]

    # Edge case: Cannot return any rows because of tokens so return an empty string
    if len(truncated_df) == 0:
        return ""

    truncated_result = _to_json_string(truncated_df)

    # Double-check edge case if we overshot by one
    if _count_tokens(truncated_result) > MAX_TOKENS_OF_DATA:
        truncated_result = _to_json_string(truncated_df.iloc[:-1])

    return truncated_result.strip()


class Genie:
    def __init__(self, space_id, client: Optional["WorkspaceClient"] = None):
        self.space_id = space_id
        workspace_client = client or WorkspaceClient()
        self.genie = workspace_client.genie
        self.description = self.genie.get_space(space_id).description
        self.headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    @mlflow.trace()
    def start_conversation(self, content):
        resp = self.genie._api.do(
            "POST",
            f"/api/2.0/genie/spaces/{self.space_id}/start-conversation",
            body={"content": content},
            headers=self.headers,
        )
        return resp

    @mlflow.trace()
    def create_message(self, conversation_id, content):
        resp = self.genie._api.do(
            "POST",
            f"/api/2.0/genie/spaces/{self.space_id}/conversations/{conversation_id}/messages",
            body={"content": content},
            headers=self.headers,
        )
        return resp

    @mlflow.trace()
    def poll_for_result(self, conversation_id, message_id, return_data_as_json: bool = False):
        @mlflow.trace()
        def poll_query_results(attachment_id, query_str, description, conversation_id=conversation_id):
            iteration_count = 0
            while iteration_count < MAX_ITERATIONS:
                iteration_count += 1
                resp = self.genie._api.do(
                    "GET",
                    f"/api/2.0/genie/spaces/{self.space_id}/conversations/{conversation_id}/messages/{message_id}/attachments/{attachment_id}/query-result",
                    headers=self.headers,
                )["statement_response"]
                state = resp["status"]["state"]
                returned_conversation_id = resp.get("conversation_id", None)
                if state == "SUCCEEDED":
                    do_parse = _parse_query_result_json if return_data_as_json is True else _parse_query_result
                    result = do_parse(resp)
                    return GenieResponse(result, query_str, description, returned_conversation_id)
                elif state in ["RUNNING", "PENDING"]:
                    logging.debug("Waiting for query result...")
                    time.sleep(5)
                else:
                    return GenieResponse(
                        f"No query result: {resp['state']}", query_str, description, returned_conversation_id
                    )
            return GenieResponse(
                f"Genie query for result timed out after {MAX_ITERATIONS} iterations of 5 seconds",
                query_str,
                description,
                conversation_id
            )

        @mlflow.trace()
        def poll_result():
            iteration_count = 0
            while iteration_count < MAX_ITERATIONS:
                iteration_count += 1
                resp = self.genie._api.do(
                    "GET",
                    f"/api/2.0/genie/spaces/{self.space_id}/conversations/{conversation_id}/messages/{message_id}",
                    headers=self.headers,
                )
                returned_conversation_id = resp.get("conversation_id", None)
                if resp["status"] == "COMPLETED":
                    attachment = next((r for r in resp["attachments"] if "query" in r), None)
                    if attachment:
                        query_obj = attachment["query"]
                        description = query_obj.get("description", "")
                        query_str = query_obj.get("query", "")
                        attachment_id = attachment["attachment_id"]
                        return poll_query_results(attachment_id, query_str, description, returned_conversation_id)
                    if resp["status"] == "COMPLETED":
                        text_content = next(r for r in resp["attachments"] if "text" in r)["text"][
                            "content"
                        ]
                        return GenieResponse(result=text_content, conversation_id=returned_conversation_id)
                elif resp["status"] in {"CANCELLED", "QUERY_RESULT_EXPIRED"}:
                    return GenieResponse(result=f"Genie query {resp['status'].lower()}.")
                elif resp["status"] == "FAILED":
                    return GenieResponse(
                        result=f"Genie query failed with error: {resp.get('error', 'Unknown error')}"
                    )
                # includes EXECUTING_QUERY, Genie can retry after this status
                else:
                    logging.debug(f"Waiting...: {resp['status']}")
                    time.sleep(5)
            return GenieResponse(
                f"Genie query timed out after {MAX_ITERATIONS} iterations of 5 seconds",
                conversation_id=conversation_id
            )

        return poll_result()

    @mlflow.trace()
    def ask_question(self, question, conversation_id: Optional[str] = None, result_as_json: bool = False):
        # check if a conversation_id is supplied
        # if yes, continue an existing genie conversation
        # otherwise start a new conversation
        if not conversation_id:
            resp = self.start_conversation(question)
        else:
            resp = self.create_message(conversation_id, question)
        genie_response =  self.poll_for_result(resp["conversation_id"], resp["message_id"], result_as_json)
        if not genie_response.conversation_id:
            genie_response.conversation_id = resp["conversation_id"]
        return genie_response
