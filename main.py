from flask import Flask, request, jsonify, Response, make_response
import instaloader
import os
import re
import requests
from urllib.parse import urlparse
import logging
import hashlib
from datetime import datetime, timedelta
import io
import time
import json
from werkzeug.middleware.proxy_fix import ProxyFix

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
# Fix for running behind a proxy
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

# Configure Instaloader instance
L = instaloader.Instaloader(
    download_video_thumbnails=False,
    download_geotags=False,
    download_comments=False,
    save_metadata=False,
    compress_json=False,
    post_metadata_txt_pattern="",
    max_connection_attempts=3
)

# Function to load cookies from file for authentication
def load_session_from_file(filename):
    try:
        with open(filename, 'r') as f:
            cookies = json.load(f)
            
        cookie_dict = {}
        for cookie in cookies:
            if cookie.get('domain', '').endswith('instagram.com'):
                cookie_dict[cookie.get('name')] = cookie.get('value')
        
        # Create a session with the cookies
        session = requests.Session()
        session.cookies.update(cookie_dict)
        
        # Import session into Instaloader
        L.context._session = session
        
        # Try to verify login status
        try:
            test_profile = instaloader.Profile.from_username(L.context, "instagram")
            logger.info(f"Successfully logged in with cookies")
            return True
        except Exception as e:
            logger.error(f"Cookie validation failed: {e}")
            return False
            
    except Exception as e:
        logger.error(f"Failed to load cookies: {e}")
        return False

# Try to authenticate with cookies if available
cookie_login_success = False
if os.path.exists('cookie.json'):
    cookie_login_success = load_session_from_file('cookie.json')
    if cookie_login_success:
        logger.info("Successfully authenticated with Instagram using cookies")
    else:
        logger.warning("Cookie authentication failed, continuing in login-less mode")
else:
    logger.warning("No cookie.json file found, running in login-less mode")

# Retry decorator for handling rate limits
def retry_with_backoff(max_retries=3, initial_backoff=2):
    def decorator(func):
        def wrapper(*args, **kwargs):
            retries = 0
            backoff = initial_backoff
            
            while retries < max_retries:
                try:
                    return func(*args, **kwargs)
                except instaloader.exceptions.InstaloaderException as e:
                    if "429" in str(e) or "rate limit" in str(e).lower() or "please wait" in str(e).lower():
                        retries += 1
                        if retries >= max_retries:
                            raise
                        
                        sleep_time = backoff * (2 ** (retries - 1))
                        logger.warning(f"Rate limited. Retrying in {sleep_time} seconds... (Attempt {retries}/{max_retries})")
                        time.sleep(sleep_time)
                    else:
                        raise
        return wrapper
    return decorator

def extract_shortcode_from_url(url):
    """Extract the Instagram shortcode from a URL."""
    # Parse URL path
    path = urlparse(url).path
    
    # Strip trailing slash if present
    if path.endswith('/'):
        path = path[:-1]
    
    # Get the last part of the path which should be the shortcode
    shortcode = path.split('/')[-1]
    
    return shortcode

# Alternative method to get post data without login
def get_post_data_no_login(url):
    """Get basic data about an Instagram post without requiring login."""
    try:
        # Extract shortcode from URL
        shortcode = extract_shortcode_from_url(url)
        logger.info(f"Extracted shortcode: {shortcode}")
        
        # Use instagram-scraper instead of full Instaloader API
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1",
            "Pragma": "no-cache",
            "Cache-Control": "no-cache"
        }
        
        # Fetch the Instagram page
        response = requests.get(f"https://www.instagram.com/p/{shortcode}/embed/", headers=headers)
        response.raise_for_status()
        
        html_content = response.text
        
        # Basic data we can extract from embed page
        post_data = {
            "shortcode": shortcode,
            "urls": {}
        }
        
        # Try to determine if it's video
        is_video = 'video' in html_content.lower() and 'poster=' in html_content.lower()
        post_data["is_video"] = is_video
        
        # Try to get username
        username_match = re.search(r'@([A-Za-z0-9._]+)', html_content)
        if username_match:
            username = username_match.group(1)
            post_data["owner"] = {
                "username": username,
                "profile_url": f"https://www.instagram.com/{username}/"
            }
        
        # Try to extract caption
        caption_match = re.search(r'<p>(.*?)</p>', html_content, re.DOTALL)
        if caption_match:
            caption = caption_match.group(1)
            # Clean HTML tags
            caption = re.sub(r'<.*?>', '', caption)
            post_data["caption"] = caption
        
        # Construct URLs for our API endpoints
        base_url = request.host_url.rstrip('/')
        post_data["urls"]["embed"] = f"https://www.instagram.com/p/{shortcode}/embed/"
        post_data["urls"]["instagram"] = f"https://www.instagram.com/p/{shortcode}/"
        
        # Add media URL if we can find it (works better for images than videos)
        img_match = re.search(r'<img[^>]+src="([^"]+)"[^>]+class="[^"]*(?:EmbeddedMediaImage|post-media)[^"]*"', html_content)
        if img_match:
            post_data["urls"]["image"] = img_match.group(1)
        
        video_match = re.search(r'<video[^>]+poster="([^"]+)"', html_content)
        if video_match:
            post_data["urls"]["thumbnail"] = video_match.group(1)
        
        # Note: We can't reliably get video URLs without login, but we can provide embed page
        
        return post_data, None
    
    except Exception as e:
        logger.error(f"Error fetching post data: {e}")
        return None, f"Error: {str(e)}"

@retry_with_backoff()
def get_post_data(url):
    """Get comprehensive data about an Instagram post."""
    try:
        # Extract shortcode from URL
        shortcode = extract_shortcode_from_url(url)
        logger.info(f"Extracted shortcode: {shortcode}")
        
        # Get post by shortcode
        post = instaloader.Post.from_shortcode(L.context, shortcode)
        
        # Base post data
        post_data = {
            "id": post.mediaid,
            "shortcode": post.shortcode,
            "is_video": post.is_video,
            "title": post.title,
            "caption": post.caption,
            "date_posted": post.date_local.isoformat(),
            "likes": post.likes,
            "comments_count": post.comments,
            "video_duration": post.video_duration if post.is_video else None,
            "location": post.location,
            "hashtags": list(post.caption_hashtags) if post.caption else [],
            "mentions": list(post.caption_mentions) if post.caption else [],
            "is_sponsored": post.is_sponsored,
            "urls": {},
            "owner": {
                "username": post.owner_username,
                "id": post.owner_id,
                "profile_url": f"https://www.instagram.com/{post.owner_username}/"
            }
        }
        
        # Add URLs
        if post.is_video:
            post_data["urls"]["video"] = post.video_url
            post_data["urls"]["thumbnail"] = post.url
            
            # Generate direct URL (no download token needed now that we're streaming)
            base_url = request.host_url.rstrip('/')
            post_data["urls"]["download"] = f"{base_url}/api/media/stream/{post.shortcode}"
        else:
            post_data["urls"]["image"] = post.url
            post_data["urls"]["download"] = f"{request.host_url.rstrip('/')}/api/media/stream/{post.shortcode}"
        
        post_data["urls"]["instagram"] = f"https://www.instagram.com/p/{post.shortcode}/"
        
        # Try to add additional owner information, but don't fail if it's not available
        try:
            owner_profile = post.owner_profile
            post_data["owner"].update({
                "is_verified": owner_profile.is_verified if hasattr(owner_profile, 'is_verified') else None,
                "full_name": owner_profile.full_name if hasattr(owner_profile, 'full_name') else None,
                "biography": owner_profile.biography if hasattr(owner_profile, 'biography') else None,
                "followers_count": owner_profile.followers if hasattr(owner_profile, 'followers') else None,
                "following_count": owner_profile.followees if hasattr(owner_profile, 'followees') else None,
                "profile_pic_url": owner_profile.profile_pic_url if hasattr(owner_profile, 'profile_pic_url') else None
            })
        except Exception as profile_error:
            logger.warning(f"Could not fetch complete profile data: {profile_error}")
            
            # Try to get at least the profile picture directly
            try:
                profile = instaloader.Profile.from_username(L.context, post.owner_username)
                post_data["owner"]["profile_pic_url"] = profile.profile_pic_url
            except Exception as pic_error:
                logger.warning(f"Could not fetch profile picture: {pic_error}")
        
        return post_data, None
    
    except instaloader.exceptions.InstaloaderException as e:
        logger.error(f"Instaloader error: {e}")
        return None, f"Instaloader error: {str(e)}"
    except Exception as e:
        logger.error(f"General error: {e}")
        return None, f"Error: {str(e)}"

def stream_media(url, content_type):
    """Stream media from URL without saving to disk."""
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
        }
        
        # Stream the content from the original URL
        req = requests.get(url, headers=headers, stream=True)
        req.raise_for_status()
        
        # Create a generator to stream the content
        def generate():
            for chunk in req.iter_content(chunk_size=8192):
                yield chunk
                
        # Return a streaming response
        return Response(
            generate(),
            content_type=content_type,
            headers={
                'Content-Disposition': 'attachment'
            }
        )
    except Exception as e:
        logger.error(f"Streaming error: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/media/stream/<shortcode>', methods=['GET'])
def stream_media_by_shortcode(shortcode):
    """Stream media directly by shortcode."""
    try:
        # Get post data first
        url = f"https://www.instagram.com/p/{shortcode}/"
        post_data, error = get_post_data(url)
        
        if error:
            return jsonify({"error": error}), 500
        
        # Determine content type and URL
        if post_data["is_video"]:
            content_type = "video/mp4"
            media_url = post_data["urls"]["video"]
        else:
            content_type = "image/jpeg"
            media_url = post_data["urls"]["image"]
        
        # Stream the media
        return stream_media(media_url, content_type)
    
    except Exception as e:
        logger.error(f"Streaming error: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/data', methods=['GET'])
def get_data():
    """API endpoint to get comprehensive data about an Instagram post."""
    # Get URL from query parameter
    url = request.args.get('url')
    
    if not url:
        return jsonify({"error": "URL parameter is required"}), 400
    
    # Validate Instagram URL
    if not re.match(r'^https?://(www\.)?instagram\.com/(p|reel|tv)/[^/]+/?.*$', url):
        return jsonify({"error": "Invalid Instagram URL"}), 400
    
    # Try first with Instaloader
    post_data, error = get_post_data(url)
    
    # If we get 401 unauthorized, try the no-login approach
    if error and ('401' in error or 'login' in error.lower() or 'authenticate' in error.lower()):
        logger.info("Trying no-login approach since authentication failed")
        post_data, new_error = get_post_data_no_login(url)
        if new_error:
            return jsonify({
                "status": "error", 
                "error": new_error,
                "message": "Instagram authentication failed. Please check credentials.",
                "status_code": "auth_error"
            }), 500
    elif error:
        # Check if it's a rate limit error
        if "Please wait a few minutes before you try again" in error or "429" in error:
            return jsonify({
                "status": "limited",
                "message": "Instagram is rate limiting our requests. Please try again in a few minutes.",
                "error": error
            }), 429
        else:
            return jsonify({
                "status": "error", 
                "error": error,
                "message": "Error fetching Instagram data.",
                "status_code": "general_error"
            }), 500
    
    # Return data
    return jsonify({
        "status": "success",
        "data": post_data
    })

@app.route('/api/direct-data', methods=['GET'])
def get_direct_data():
    """API endpoint to get direct media URLs without full post details."""
    # Get URL from query parameter
    url = request.args.get('url')
    
    if not url:
        return jsonify({"error": "URL parameter is required"}), 400
    
    # Validate Instagram URL
    if not re.match(r'^https?://(www\.)?instagram\.com/(p|reel|tv)/[^/]+/?.*$', url):
        return jsonify({"error": "Invalid Instagram URL"}), 400
    
    # Extract shortcode
    shortcode = extract_shortcode_from_url(url)
    
    # Try to get data using no-login approach
    post_data, error = get_post_data_no_login(url)
    
    if error:
        return jsonify({"status": "error", "error": error}), 500
    
    # Return simplified response
    return jsonify({
        "status": "success",
        "data": post_data
    })

@app.route('/api/embed/<shortcode>', methods=['GET'])
def get_embed(shortcode):
    """Return embed HTML for an Instagram post."""
    try:
        # Fetch the embed HTML from Instagram
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
        }
        
        response = requests.get(f"https://www.instagram.com/p/{shortcode}/embed/", headers=headers)
        response.raise_for_status()
        
        # Return the embed HTML
        return response.text
    
    except Exception as e:
        logger.error(f"Embed error: {e}")
        return jsonify({"error": str(e)}), 500

# Health check endpoint
@app.route('/health', methods=['GET'])
def health_check():
    return jsonify({
        "status": "ok", 
        "version": "1.2.0",
        "loginless_mode": not cookie_login_success
    })

# Simple web interface
@app.route('/', methods=['GET'])
def index():
    login_status = "Authenticated with cookies" if cookie_login_success else "Running in login-less mode"
    
    return f'''
    <!DOCTYPE html>
    <html>
    <head>
        <title>Instagram Data API</title>
        <style>
            body {{ font-family: Arial, sans-serif; max-width: 800px; margin: 0 auto; padding: 20px; }}
            .form-group {{ margin-bottom: 15px; }}
            label {{ display: block; margin-bottom: 5px; }}
            input[type="text"] {{ width: 100%; padding: 8px; box-sizing: border-box; }}
            .button-group {{ display: flex; gap: 10px; margin-bottom: 20px; }}
            button {{ padding: 10px 15px; background: #3897f0; color: white; border: none; cursor: pointer; }}
            button:hover {{ background: #2676d9; }}
            .result {{ margin-top: 20px; padding: 15px; border: 1px solid #ccc; border-radius: 5px; }}
            pre {{ background: #f4f4f4; padding: 10px; overflow: auto; }}
            .profile-container {{ display: flex; align-items: center; margin-top: 15px; }}
            .profile-pic {{ width: 50px; height: 50px; border-radius: 50%; margin-right: 15px; }}
            .profile-info {{ flex: 1; }}
            .status {{ padding: 10px; margin-bottom: 15px; border-radius: 5px; }}
            .status.info {{ background-color: #d1ecf1; border: 1px solid #bee5eb; color: #0c5460; }}
            .tabs {{ display: flex; margin-bottom: 20px; }}
            .tab {{ padding: 10px 15px; background: #f0f0f0; cursor: pointer; border: 1px solid #ccc; }}
            .tab.active {{ background: #3897f0; color: white; border-color: #3897f0; }}
            .embed-container {{ margin-top: 20px; }}
            iframe {{ border: none; width: 100%; height: 600px; }}
        </style>
    </head>
    <body>
        <h1>Instagram Data API</h1>
        
        <div id="status-message" class="status info">
            Status: {login_status}. {'' if cookie_login_success else 'Some features may be limited. For videos, use the embed approach.'}
        </div>
        
        <div class="form-group">
            <label for="insta-url">Enter Instagram URL:</label>
            <input type="text" id="insta-url" placeholder="https://www.instagram.com/p/SHORTCODE/">
        </div>
        
        <div class="tabs">
            <div class="tab active" onclick="switchTab('data')">Get Data</div>
            <div class="tab" onclick="switchTab('embed')">View Embed</div>
        </div>
        
        <div id="data-tab">
            <div class="button-group">
                <button onclick="fetchData()">Get Media Data</button>
                <button onclick="fetchDirectData()">Get Basic Data</button>
            </div>
        </div>
        
        <div id="embed-tab" style="display:none;">
            <button onclick="showEmbed()">Load Embed</button>
            <div class="embed-container" id="embed-container"></div>
        </div>
        
        <div id="result" class="result" style="display:none;"></div>
        
        <script>
            function switchTab(tab) {{
                // Hide all tabs
                document.getElementById('data-tab').style.display = 'none';
                document.getElementById('embed-tab').style.display = 'none';
                
                // Show selected tab
                document.getElementById(tab + '-tab').style.display = 'block';
                
                // Update active tab styling
                const tabs = document.querySelectorAll('.tab');
                tabs.forEach(t => t.classList.remove('active'));
                
                // Find the clicked tab and make it active
                event.target.classList.add('active');
            }}
            
            function getShortcode() {{
                const url = document.getElementById('insta-url').value;
                if (!url) {{
                    alert('Please enter an Instagram URL');
                    return null;
                }}
                
                // Extract shortcode
                const regex = /instagram\\.com\\/(p|reel|tv)\\/([^\\/\\?]+)/;
                const match = url.match(regex);
                
                if (match && match[2]) {{
                    return match[2];
                }} else {{
                    alert('Invalid Instagram URL');
                    return null;
                }}
            }}
            
            function showEmbed() {{
                const shortcode = getShortcode();
                if (!shortcode) return;
                
                const container = document.getElementById('embed-container');
                container.innerHTML = `<iframe src="https://www.instagram.com/p/${{shortcode}}/embed/"></iframe>`;
            }}
            
            function fetchData() {{
                const url = document.getElementById('insta-url').value;
                if (!url) {{
                    alert('Please enter an Instagram URL');
                    return;
                }}
                
                const resultDiv = document.getElementById('result');
                resultDiv.innerHTML = '<p>Loading data...</p>';
                resultDiv.style.display = 'block';
                
                fetch(`/api/data?url=${{encodeURIComponent(url)}}`)
                    .then(response => response.json())
                    .then(data => {{
                        let profilePicHtml = '';
                        if (data.status === 'success' && data.data.owner && data.data.owner.profile_pic_url) {{
                            profilePicHtml = `
                                <div class="profile-container">
                                    <img src="${{data.data.owner.profile_pic_url}}" class="profile-pic" alt="Profile Picture">
                                    <div class="profile-info">
                                        <strong>${{data.data.owner.username}}</strong>
                                        ${{data.data.owner.full_name ? `<p>${{data.data.owner.full_name}}</p>` : ''}}
                                    </div>
                                </div>
                            `;
                        }}
                        
                        let mediaPreview = '';
                        if (data.status === 'success') {{
                            if (data.data.is_video && data.data.urls.video) {{
                                mediaPreview = `
                                    <div style="margin-top: 15px;">
                                        <video controls style="max-width: 100%; max-height: 400px;">
                                            <source src="${{data.data.urls.video}}" type="video/mp4">
                                            Your browser does not support the video tag.
                                        </video>
                                    </div>
                                `;
                            }} else if (!data.data.is_video && data.data.urls.image) {{
                                mediaPreview = `
                                    <div style="margin-top: 15px;">
                                        <img src="${{data.data.urls.image}}" style="max-width: 100%; max-height: 400px;" alt="Instagram Image">
                                    </div>
                                `;
                            }}
                        }}
                        
                        resultDiv.innerHTML = `
                            ${{profilePicHtml}}
                            ${{mediaPreview}}
                            <h3>API Response:</h3>
                            <pre>${{JSON.stringify(data, null, 2)}}</pre>
                            ${{data.status === 'success' && data.data.urls.download ? 
                                `<p><a href="${{data.data.urls.download}}" target="_blank">Download Media</a></p>` : ''}}
                        `;
                    }})
                    .catch(error => {{
                        resultDiv.innerHTML = `<p>Error: ${{error.message}}</p>`;
                    }});
            }}
            
            function fetchDirectData() {{
                const url = document.getElementById('insta-url').value;
                if (!url) {{
                    alert('Please enter an Instagram URL');
                    return;
                }}
                
                const resultDiv = document.getElementById('result');
                resultDiv.innerHTML = '<p>Loading data...</p>';
                resultDiv.style.display = 'block';
                
                fetch(`/api/direct-data?url=${{encodeURIComponent(url)}}`)
                    .then(response => response.json())
                    .then(data => {{
                        let mediaPreview = '';
                        if (data.status === 'success') {{
                            if (data.data.urls.image) {{
                                mediaPreview = `
                                    <div style="margin-top: 15px;">
                                        <img src="${{data.data.urls.image}}" style="max-width: 100%; max-height: 400px;" alt="Instagram Image">
                                    </div>
                                `;
                            }} else if (data.data.urls.thumbnail) {{
                                mediaPreview = `
                                    <div style="margin-top: 15px;">
                                        <img src="${{data.data.urls.thumbnail}}" style="max-width: 100%; max-height: 400px;" alt="Video Thumbnail">
                                        <p><em>Video thumbnail shown. Use embed tab to view the actual video.</em></p>
                                    </div>
                                `;
                            }}
                        }}
                        
                        resultDiv.innerHTML = `
                            ${{mediaPreview}}
                            <h3>API Response:</h3>
                            <pre>${{JSON.stringify(data, null, 2)}}</pre>
                        `;
                    }})
                    .catch(error => {{
                        resultDiv.innerHTML = `<p>Error: ${{error.message}}</p>`;
                    }});
            }}
        </script>
    </body>
    </html>
    '''

# For deployment
if __name__ == '__main__':
    # Get port from environment variable or use default
    port = int(os.environ.get('PORT', 8080))
    
    # Use Gunicorn or another WSGI server in production
    if os.environ.get('FLASK_ENV') == 'development':
        app.run(host='0.0.0.0', port=port, debug=True)
    else:
        app.run(host='0.0.0.0', port=port)
