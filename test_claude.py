import anthropic
from dotenv import load_dotenv
import os

load_dotenv()

c = anthropic.Anthropic(api_key=os.getenv('ANTHROPIC_API_KEY'))
m = c.messages.create(
    model='claude-sonnet-4-6',
    max_tokens=10,
    messages=[{'role': 'user', 'content': 'say hi'}]
)
print(m.content)
