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


INTRODUCE_ASSISTANT = "You are a virtual assistant that can help with a variety of tasks"