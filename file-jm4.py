import asyncio
import aiohttp
import aiofiles
from datetime import datetime
from dataclasses import dataclass
from typing import List, Optional
import random

@dataclass
class TokenBucket:
    token: str
    last_used: float = 0
    rate_limited_until: float = 0
    failure_count: int = 0

class KeyChecker:
    def __init__(self, tokens: List[str], delay_between_requests: float = 7.5, max_concurrent: int = 5):
        self.tokens = [TokenBucket(t) for t in tokens]
        self.delay = delay_between_requests
        self.semaphore = asyncio.Semaphore(max_concurrent)
        self.results = []
        self.lock = asyncio.Lock()
        
    def get_available_token(self) -> Optional[TokenBucket]:
        """Get the next available token that isn't rate limited"""
        now = asyncio.get_event_loop().time()
        # Sort by last used time (oldest first)
        available = [t for t in self.tokens if now - t.rate_limited_until >= 0]
        if not available:
            return None
        return min(available, key=lambda t: t.last_used)
    
    async def check_key(self, session: aiohttp.ClientSession, key: str, output_file: str):
        """Check a single key with retry logic"""
        async with self.semaphore:  # Limit concurrent requests
            max_retries = 3
            retry_delay = 2
            
            for attempt in range(max_retries):
                token_bucket = self.get_available_token()
                
                if token_bucket is None:
                    # All tokens rate limited, wait a bit
                    await asyncio.sleep(5)
                    continue
                
                now = asyncio.get_event_loop().time()
                elapsed = now - token_bucket.last_used
                
                if elapsed < self.delay:
                    await asyncio.sleep(self.delay - elapsed)
                
                token_bucket.last_used = asyncio.get_event_loop().time()
                
                url = f"https://purchase.mp.microsoft.com/v7.0/tokenDescriptions/{key}?market=US&language=en-US"
                headers = {"Authorization": token_bucket.token}
                
                try:
                    async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=30)) as response:
                        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                        
                        if response.status == 429:
                            # Mark token as rate limited for 60 seconds
                            token_bucket.rate_limited_until = asyncio.get_event_loop().time() + 60
                            token_bucket.failure_count += 1
                            if attempt < max_retries - 1:
                                await asyncio.sleep(retry_delay * (attempt + 1))
                                continue
                            result = f"{key} | ERROR: RateLimited | Checked at: {timestamp} CEST"
                            print(f"Rate limited on {key[:8]}... (token marked for cooldown)")
                            
                        elif response.status == 401:
                            result = f"{key} | ERROR: Invalid Auth Token | Checked at: {timestamp} CEST"
                            token_bucket.failure_count += 1
                            
                        elif response.status == 403:
                            result = f"{key} | ERROR: Wrong Country | Checked at: {timestamp} CEST"
                            
                        elif response.status == 404:
                            result = f"{key} | ERROR: Invalid key | Checked at: {timestamp} CEST"
                            
                        elif response.status == 200:
                            data = await response.json()
                            key_state = data.get("tokenState", "Unknown")
                            if key_state == "Active":
                                key_state = "Not Redeemed"
                            result = f"{key} | Key State: {key_state} | Checked at: {timestamp} CEST"
                            print(f"{key_state} @ {timestamp} ({key[:8]}...)")
                            token_bucket.failure_count = 0  # Reset on success
                        else:
                            result = f"{key} | ERROR: HTTP {response.status} | Checked at: {timestamp} CEST"
                        
                        async with self.lock:
                            async with aiofiles.open(output_file, "a") as f:
                                await f.write(result + "\n")
                        return
                        
                except asyncio.TimeoutError:
                    if attempt < max_retries - 1:
                        await asyncio.sleep(retry_delay)
                        continue
                    result = f"{key} | ERROR: Timeout | Checked at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} CEST"
                    async with self.lock:
                        async with aiofiles.open(output_file, "a") as f:
                            await f.write(result + "\n")
                            
                except Exception as e:
                    if attempt < max_retries - 1:
                        await asyncio.sleep(retry_delay)
                        continue
                    result = f"{key} | ERROR: {str(e)} | Checked at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} CEST"
                    async with self.lock:
                        async with aiofiles.open(output_file, "a") as f:
                            await f.write(result + "\n")

    async def run(self, keys: List[str], output_file: str):
        """Run all checks concurrently"""
        connector = aiohttp.TCPConnector(limit=100, limit_per_host=30)
        timeout = aiohttp.ClientTimeout(total=60)
        
        async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
            tasks = [self.check_key(session, key, output_file) for key in keys]
            await asyncio.gather(*tasks, return_exceptions=True)

async def main():
    # Read keys
    try:
        with open('keys.txt', 'r') as f:
            keys = [line.strip() for line in f if line.strip()]
    except FileNotFoundError:
        print("keys.txt not found!")
        return
    
    # Read tokens
    try:
        with open('token.txt', 'r') as f:
            tokens = [line.strip() for line in f if line.strip()]
    except FileNotFoundError:
        print("token.txt not found!")
        return
    
    if not tokens:
        print("No tokens found!")
        return
    
    if not keys:
        print("No keys found!")
        return
    
    print(f"Loaded {len(keys)} keys and {len(tokens)} tokens")
    output_filename = input("Enter the output file name (e.g., output.txt): ")
    
    # Clear/create output file
    with open(output_filename, "w") as f:
        pass
    
    # Calculate optimal concurrency
    # With 7.5s delay per token, we can make ~8 requests per token per minute
    # So with N tokens, we can safely do N concurrent requests
    max_concurrent = min(len(tokens) * 2, 20)  # Cap at 20 to be safe
    
    checker = KeyChecker(tokens, delay_between_requests=7.5, max_concurrent=max_concurrent)
    
    start_time = datetime.now()
    print(f"Starting check at {start_time.strftime('%H:%M:%S')}")
    print(f"Estimated time: ~{len(keys) * 7.5 / len(tokens) / 60:.1f} minutes")
    
    await checker.run(keys, output_filename)
    
    end_time = datetime.now()
    duration = (end_time - start_time).total_seconds()
    print(f"\nCompleted in {duration:.1f} seconds")
    print(f"Average: {len(keys)/duration:.2f} keys/second")

if __name__ == "__main__":
    asyncio.run(main())