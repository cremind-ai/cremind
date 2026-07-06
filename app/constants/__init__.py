from enum import Enum


class ChatCompletionTypeEnum(Enum):
    CONTENT = 0
    FUNCTION_CALLING = 1
    THINK = 2
    CLARIFY = 3
    DONE = 4
    ERROR = 5
    TIMEOUT = 6
    STATUS_UPDATE = 7
    THINKING_ARTIFACT = 8
    RESULT_ARTIFACT = 9
    # Plan-mode UI signal (ask_user_question / plan_ready / todos). Carries an
    # ``event`` discriminator; ``stream_runner`` translates it to a bus event.
    PLAN_EVENT = 10


INTRODUCE_ASSISTANT = "You are a virtual assistant that can help with a variety of tasks"