
import dotenv
from langchain_aws import ChatBedrock
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain_openai import ChatOpenAI
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode
from prompts import PR_COMMENT_PROMPT, PR_FETCHER_PROMPT, REPO_ANALYZER_PROMPT
from tenacity import retry, stop_after_attempt, wait_exponential
from tools import DiffFormatter, get_pr_diff, get_pr_metadata

from composio_langgraph import Action, App, ComposioToolSet, WorkspaceType


dotenv.load_dotenv()


model = Model.OPENAI


def add_thought_to_request(request: t.Dict[str, t.Any]) -> t.Dict[str, t.Any]:
    request["thought"] = {
        "type": "string",
        "description": "Provide the thought of the agent in a small paragraph in concise way. This is a required field.",
        "required": True,
    }
    return request


def pop_thought_from_request(request: t.Dict[str, t.Any]) -> t.Dict[str, t.Any]:
    request.pop("thought", None)
    return request


def _github_pulls_create_review_comment_post_proc(response: dict) -> dict:
    if response["successfull"]:
        return {"message": "commented sucessfully"}
    return {"error": response["error"]}


def _github_list_commits_post_proc(response: dict) -> dict:
    if not response["successfull"]:
        return {"error": response["error"]}
    commits = []
    for commit in response.get("data", {}).get("details", []):
        commits.append(
            {
                "sha": commit["sha"],
                "author": commit["commit"]["author"]["name"],
                "message": commit["commit"]["message"],
                "date": commit["commit"]["author"]["date"],
            }
        )
    return {"commits": commits}


def _github_diff_post_proc(response: dict) -> dict:
    if not response["successfull"]:
        return {"error": response["error"]}
    return {"diff": DiffFormatter(response["data"]["details"]).parse_and_format()}


def _github_get_a_pull_request_post_proc(response: dict):
    if not response["successfull"]:
        return {"error": response["error"]}
    pr_content = response.get("data", {}).get("details", [])
    contents = pr_content.split("\n\n---")
    pr_content = ""
    for i, content in enumerate(contents):
        if "diff --git" in content:
            index = content.index("diff --git")
            content_filtered = content[:index]
            if i != len(contents) - 1:
                content_filtered += "\n".join(content.splitlines()[-4:])
        else:
            content_filtered = content
        pr_content += content_filtered
    return {
        "details": pr_content,
        "message": "PR content fetched successfully, proceed with getting the diff of PR or individual commits",
    }


def _github_list_review_comments_on_a_pull_request_post_proc(response: dict) -> dict:
    if not response["successfull"]:
        return {"error": response["error"]}
    comments = []
    for comment in response.get("data", {}).get("details", []):
        comments.append(
            {
                "diff_hunk": comment["diff_hunk"],
                "commit_id": comment["commit_id"],
                "body": comment["body"],
            }
        )
    return {"comments": comments}


def get_graph(repo_path):
    toolset = ComposioToolSet(
        workspace_config=WorkspaceType.Host(persistent=True),  # WorkspaceType.Docker(persistent=True),
        metadata={
            App.CODE_ANALYSIS_TOOL: {
                "dir_to_index_path": repo_path,
            }
        },
        processors={
            "pre": {
                App.GITHUB: pop_thought_from_request,
                App.FILETOOL: pop_thought_from_request,
                App.CODE_ANALYSIS_TOOL: pop_thought_from_request,
            },
            "schema": {
                App.GITHUB: add_thought_to_request,
                App.FILETOOL: add_thought_to_request,
                App.CODE_ANALYSIS_TOOL: add_thought_to_request,
            },
            "post": {
                Action.GITHUB_CREATE_AN_ISSUE_COMMENT: _github_pulls_create_review_comment_post_proc,
                Action.GITHUB_CREATE_A_REVIEW_COMMENT_FOR_A_PULL_REQUEST: _github_pulls_create_review_comment_post_proc,
                Action.GITHUB_LIST_COMMITS_ON_A_PULL_REQUEST: _github_list_commits_post_proc,
                Action.GITHUB_GET_A_COMMIT: _github_diff_post_proc,
                Action.GITHUB_GET_A_PULL_REQUEST: _github_get_a_pull_request_post_proc,
                Action.GITHUB_LIST_REVIEW_COMMENTS_ON_A_PULL_REQUEST: _github_list_review_comments_on_a_pull_request_post_proc,
            },
        },
    )

    fetch_pr_tools = [
        *toolset.get_tools(
            actions=[
                Action.GITHUB_GET_A_PULL_REQUEST,
                Action.GITHUB_LIST_COMMITS_ON_A_PULL_REQUEST,
                Action.GITHUB_GET_A_COMMIT,
                get_pr_diff,
                get_pr_metadata,
            ]
        )
    ]

    repo_analyzer_tools = [
        *toolset.get_tools(
            actions=[
                Action.CODE_ANALYSIS_TOOL_GET_CLASS_INFO,
                Action.CODE_ANALYSIS_TOOL_GET_METHOD_BODY,
                Action.CODE_ANALYSIS_TOOL_GET_METHOD_SIGNATURE,
                # Action.FILETOOL_LIST_FILES,
                Action.FILETOOL_OPEN_FILE,
                Action.FILETOOL_SCROLL,
                # Action.FILETOOL_FIND_FILE,
                Action.FILETOOL_SEARCH_WORD,
            ]
        )
    ]

    comment_on_pr_tools = [
        *toolset.get_tools(
            actions=[
                Action.GITHUB_GET_A_COMMIT,
                Action.GITHUB_CREATE_A_REVIEW_COMMENT_FOR_A_PULL_REQUEST,
                Action.GITHUB_CREATE_AN_ISSUE_COMMENT,
            ]
        )
    ]

    if model == Model.CLAUDE:
        client = ChatBedrock(
            credentials_profile_name="default",
            model_id="anthropic.claude-3-5-sonnet-20240620-v1:0",
            region_name="us-east-1",
            model_kwargs={"temperature": 0, "max_tokens": 8192},
        )
    else:
        client = ChatOpenAI(
            model="gpt-4",
            temperature=0,
            #max_completion_tokens=4096,
            api_key= 'ae8838e3-941d-46fd-a5c8-de28e43f6751', #os.environ["OPENAI_API_KEY"],
            base_url="http://gpt-proxy.jd.com/gateway/azure",
        )

    class AgentState(t.TypedDict):
        messages: t.Annotated[t.Sequence[BaseMessage], operator.add]
        sender: str

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=4, max=10),
    )
    def invoke_with_retry(agent, state):
        return agent.invoke(state)

    def create_agent_node(agent, name):
        def agent_node(state):
            if model == Model.CLAUDE and isinstance(state["messages"][-1], AIMessage):
                state["messages"].append(HumanMessage(content="Placeholder message"))

            try:
                result = invoke_with_retry(agent, state)
            except Exception as e:
                print(f"Failed to invoke agent after 3 attempts: {str(e)}")
                result = AIMessage(
                    content="I apologize, but I encountered an error and couldn't complete the task. Please try again or rephrase your request.",
                    name=name,
                )
            if not isinstance(result, ToolMessage):
                if isinstance(result, dict):
                    result_dict = result
                else:
                    result_dict = result.dict()
                result = AIMessage(
                    **{
                        k: v
                        for k, v in result_dict.items()
                        if k not in ["type", "name"]
                    },
                    name=name,
                )
            return {"messages": [result], "sender": name}

        return agent_node

    def create_agent(system_prompt, tools):
        prompt = ChatPromptTemplate.from_messages(
            [
                ("system", system_prompt),
                MessagesPlaceholder(variable_name="messages"),
            ]
        )
        llm = client
        if tools:
            # return prompt | llm.bind_tools(tools)
            return prompt | llm.bind_tools(tools)
        else:
            return prompt | llm

    fetch_pr_agent_name = "Fetch-PR-Agent"
    fetch_pr_agent = create_agent(PR_FETCHER_PROMPT, fetch_pr_tools)
    fetch_pr_agent_node = create_agent_node(fetch_pr_agent, fetch_pr_agent_name)

    repo_analyzer_agent_name = "Repo-Analyzer-Agent"
    repo_analyzer_agent = create_agent(REPO_ANALYZER_PROMPT, repo_analyzer_tools)
    repo_analyzer_agent_node = create_agent_node(
        repo_analyzer_agent, repo_analyzer_agent_name
    )

    comment_on_pr_agent_name = "Comment-On-PR-Agent"
    comment_on_pr_agent = create_agent(PR_COMMENT_PROMPT, comment_on_pr_tools)
    comment_on_pr_agent_node = create_agent_node(
        comment_on_pr_agent, comment_on_pr_agent_name
    )

    workflow = StateGraph(AgentState)

    workflow.add_edge(START, fetch_pr_agent_name)
    workflow.add_node(fetch_pr_agent_name, fetch_pr_agent_node)
    workflow.add_node(repo_analyzer_agent_name, repo_analyzer_agent_node)
    workflow.add_node(comment_on_pr_agent_name, comment_on_pr_agent_node)
    workflow.add_node("fetch_pr_tools_node", ToolNode(fetch_pr_tools))
    workflow.add_node("repo_analyzer_tools_node", ToolNode(repo_analyzer_tools))
    workflow.add_node("comment_on_pr_tools_node", ToolNode(comment_on_pr_tools))

    def fetch_pr_router(
        state,
    ) -> t.Literal["fetch_pr_tools_node", "continue", "analyze_repo"]:
        messages = state["messages"]
        for message in reversed(messages):
            if isinstance(message, AIMessage):
                last_ai_message = message
                break
        else:
            last_ai_message = messages[-1]

        if last_ai_message.tool_calls:
            return "fetch_pr_tools_node"
        if "ANALYZE REPO" in last_ai_message.content:
            return "analyze_repo"
        return "continue"

    workflow.add_conditional_edges(
        "fetch_pr_tools_node",
        lambda x: x["sender"],
        {fetch_pr_agent_name: fetch_pr_agent_name},
    )
    workflow.add_conditional_edges(
        fetch_pr_agent_name,
        fetch_pr_router,
        {
            "continue": fetch_pr_agent_name,
            "fetch_pr_tools_node": "fetch_pr_tools_node",
            "analyze_repo": repo_analyzer_agent_name,
        },
    )

    def repo_analyzer_router(
        state,
    ) -> t.Literal["repo_analyzer_tools_node", "continue", "comment_on_pr"]:
        messages = state["messages"]
        for message in reversed(messages):
            if isinstance(message, AIMessage):
                last_ai_message = message
                break
        else:
            last_ai_message = messages[-1]

        if last_ai_message.tool_calls:
            return "repo_analyzer_tools_node"
        if "ANALYSIS COMPLETED" in last_ai_message.content:
            return "comment_on_pr"
        return "continue"

    workflow.add_conditional_edges(
        "repo_analyzer_tools_node",
        lambda x: x["sender"],
        {repo_analyzer_agent_name: repo_analyzer_agent_name},
    )
    workflow.add_conditional_edges(
        repo_analyzer_agent_name,
        repo_analyzer_router,
        {
            "continue": repo_analyzer_agent_name,
            "repo_analyzer_tools_node": "repo_analyzer_tools_node",
            "comment_on_pr": comment_on_pr_agent_name,
        },
    )

    def comment_on_pr_router(
        state,
    ) -> t.Literal["comment_on_pr_tools_node", "continue", "analyze_repo", "__end__"]:
        messages = state["messages"]
        for message in reversed(messages):
            if isinstance(message, AIMessage):
                last_ai_message = message
                break
        else:
            last_ai_message = messages[-1]

        if last_ai_message.tool_calls:
            return "comment_on_pr_tools_node"
        if "ANALYZE REPO" in last_ai_message.content:
            return "analyze_repo"
        if "REVIEW COMPLETED" in last_ai_message.content:
            return "__end__"
        return "continue"

    workflow.add_conditional_edges(
        "comment_on_pr_tools_node",
        lambda x: x["sender"],
        {comment_on_pr_agent_name: comment_on_pr_agent_name},
    )
    workflow.add_conditional_edges(
        comment_on_pr_agent_name,
        comment_on_pr_router,
        {
            "continue": comment_on_pr_agent_name,
            "analyze_repo": repo_analyzer_agent_name,
            "comment_on_pr_tools_node": "comment_on_pr_tools_node",
            "__end__": END,
        },
    )

    graph = workflow.compile()

    return graph, toolset
