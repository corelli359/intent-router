import jwt
import time
from router_service.settings import JWT_SALT, X_APP_ID
import httpx
import logging


logger = logging.getLogger(__name__)

def generate_jwt(salt: str = '', expire_seconds: int = 3600) -> str:
    """
    生成 JWT token

    Args:
        salt: JWT 密钥，如果不提供则使用配置中的 JWT_SALT
        expire_seconds: 过期时间（秒），默认 1 小时

    Returns:
        JWT token 字符串
    """
    if not salt:
        salt = JWT_SALT

    if not salt:
        raise ValueError("JWT_SALT is not configured")

    # 生成 payload
    payload = {
        "iat": int(time.time()),  # 签发时间
        "exp": int(time.time()) + expire_seconds,  # 过期时间
        "app_id": X_APP_ID,  # 可选：将 app_id 也放入 payload
    }

    # 生成 JWT
    token = jwt.encode(payload, salt, algorithm="HS256")

    return token

class AuthHTTPClient(httpx.AsyncClient):
    """自定义 HTTP 客户端，每次请求前动态添加认证 headers"""

    async def send(self, request, *args, **kwargs):
        """重写 send 方法，在发送请求前添加认证 headers"""
        try:
            # 每次请求前生成新的 JWT
            token = generate_jwt()
            request.headers["Authorization"] = f"Bearer {token}"
            request.headers["x-app-id"] = X_APP_ID
        except Exception as e:
            logger.exception("Failed to generate JWT before outbound LLM request")
            raise

        return await super().send(request, *args, **kwargs)
