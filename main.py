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
from werkzeug.middleware.proxy_fix import ProxyFix

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
# Fix for running behind a proxy
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

# Configure Instaloader instance with login credentials
L = instaloader.Instaloader(
    download_video_thumbnails=False,
    download_geotags=False,
    download_comments=False,
    save_metadata=False,
    compress_json=False,
    post_metadata_txt_pattern="",
    max_connection_attempts=3
)

# Get Instagram credentials from environment variables
INSTA_USERNAME = os.environ.get("INSTA_USERNAME")
INSTA_PASSWORD = os.environ.get("INSTA_PASSWORD")
SESSION_FILE = "instagram_session"

# Login to Instagram
def login_to_instagram():
    try:
        # Try to load session from file
        if os.path.exists(SESSION_FILE):
            logger.info("Loading Instagram session from file...")
            L.load_session_from_file(INSTA_USERNAME, SESSION_FILE)
            return True
        # Login with username and password
        elif INSTA_USERNAME and INSTA_PASSWORD:
            logger.info(f"Logging in to Instagram as {INSTA_USERNAME}...")
            L.login(INSTA_USERNAME, INSTA_PASSWORD)
            # Save session for future use
            L.save_session_to_file(SESSION_FILE)
            return True
        else:
            logger.warning("No Instagram credentials provided. Some features may not work.")
            return False
    except Exception as e:
        logger.error(f"Instagram login failed: {e}")
        return False

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
    
    # Make sure we're logged in
    if not hasattr(L.context, "username"):
        login_success = login_to_instagram()
        if not login_success and (INSTA_USERNAME and INSTA_PASSWORD):
            return jsonify({
                "status": "error", 
                "error": "Failed to authenticate with Instagram. Check your credentials."
            }), 500
    
    # Get post data
    post_data, error = get_post_data(url)
    
    if error:
        # Check if it's a rate limit error
        if "Please wait a few minutes before you try again" in error or "429" in error:
            return jsonify({
                "status": "limited",
                "message": "Instagram is rate limiting our requests. Please try again in a few minutes.",
                "error": error
            }), 429
        elif "401" in error or "login" in error.lower() or "authenticate" in error.lower():
            # Try to login again
            login_success = login_to_instagram()
            if login_success:
                # Retry the request after logging in
                post_data, error = get_post_data(url)
                if error:
                    return jsonify({"status": "error", "error": error}), 500
            else:
                return jsonify({
                    "status": "error", 
                    "error": "Authentication required. Please set INSTA_USERNAME and INSTA_PASSWORD environment variables."
                }), 401
        else:
            return jsonify({"status": "error", "error": error}), 500
    
    # Return data
    return jsonify({
        "status": "success",
        "data": post_data
    })

@app.route('/api/media/stream/<shortcode>', methods=['GET'])
def stream_media_endpoint(shortcode):
    """API endpoint to stream Instagram media directly without saving to disk."""
    try:
        # Make sure we're logged in
        if not hasattr(L.context, "username"):
            login_success = login_to_instagram()
        
        # Get post by shortcode
        post = instaloader.Post.from_shortcode(L.context, shortcode)
        
        # For videos
        if post.is_video:
            video_url = post.video_url
            return stream_media(video_url, "video/mp4")
        # For images
        else:
            image_url = post.url
            return stream_media(image_url, "image/jpeg")
    
    except instaloader.exceptions.InstaloaderException as e:
        logger.error(f"Instaloader error: {e}")
        return jsonify({"error": f"Instaloader error: {str(e)}"}), 500
    except Exception as e:
        logger.error(f"General error: {e}")
        return jsonify({"error": f"Error: {str(e)}"}), 500

@app.route('/api/direct-data', methods=['GET'])
def get_direct_data():
    """API endpoint to get direct video URL without profile info (less prone to rate limiting)."""
    # Get URL from query parameter
    url = request.args.get('url')
    
    if not url:
        return jsonify({"error": "URL parameter is required"}), 400
    
    # Validate Instagram URL
    if not re.match(r'^https?://(www\.)?instagram\.com/(p|reel|tv)/[^/]+/?.*$', url):
        return jsonify({"error": "Invalid Instagram URL"}), 400
    
    # Make sure we're logged in
    if not hasattr(L.context, "username"):
        login_success = login_to_instagram()
    
    try:
        # Extract shortcode from URL
        shortcode = extract_shortcode_from_url(url)
        logger.info(f"Extracted shortcode: {shortcode}")
        
        # Get post by shortcode
        post = instaloader.Post.from_shortcode(L.context, shortcode)
        
        # Create direct response with just the critical data
        response_data = {
            "status": "success",
            "data": {
                "shortcode": post.shortcode,
                "is_video": post.is_video,
                "urls": {}
            }
        }
        
        # Add URLs
        if post.is_video:
            response_data["data"]["urls"]["video"] = post.video_url
            response_data["data"]["urls"]["thumbnail"] = post.url
        else:
            response_data["data"]["urls"]["image"] = post.url
        
        # Add streaming endpoint URL
        base_url = request.host_url.rstrip('/')
        response_data["data"]["urls"]["download"] = f"{base_url}/api/media/stream/{post.shortcode}"
        
        # Try to get profile picture
        try:
            profile = instaloader.Profile.from_username(L.context, post.owner_username)
            response_data["data"]["profile_pic"] = profile.profile_pic_url
        except Exception as e:
            logger.warning(f"Could not fetch profile picture: {e}")
        
        return jsonify(response_data)
        
    except Exception as e:
        logger.error(f"Error in direct-data endpoint: {e}")
        return jsonify({"status": "error", "error": str(e)}), 500

# Health check endpoint
@app.route('/health', methods=['GET'])
def health_check():
    return jsonify({
        "status": "ok", 
        "version": "1.2.0",
        "authenticated": hasattr(L.context, "username"),
        "username": L.context.username if hasattr(L.context, "username") else None
    })

# Login status and attempt endpoint
@app.route('/login', methods=['GET'])
def login_status():
    if hasattr(L.context, "username"):
        return jsonify({
            "status": "authenticated",
            "username": L.context.username
        })
    else:
        success = login_to_instagram()
        if success:
            return jsonify({
                "status": "authenticated",
                "username": L.context.username
            })
        else:
            return jsonify({
                "status": "not_authenticated",
                "message": "Could not authenticate. Check environment variables."
            }), 401

# Simple web interface
@app.route('/', methods=['GET'])
def index():
    return '''
    <!DOCTYPE html>
    <html>
    <head>
        <title>Instagram Data API</title>
        <style>
            body { font-family: Arial, sans-serif; max-width: 800px; margin: 0 auto; padding: 20px; }
            .form-group { margin-bottom: 15px; }
            label { display: block; margin-bottom: 5px; }
            input[type="text"] { width: 100%; padding: 8px; box-sizing: border-box; }
            .button-group { display: flex; gap: 10px; margin-bottom: 20px; }
            button { padding: 10px 15px; background: #3897f0; color: white; border: none; cursor: pointer; }
            button:hover { background: #2676d9; }
            .result { margin-top: 20px; padding: 15px; border: 1px solid #ccc; border-radius: 5px; }
            pre { background: #f4f4f4; padding: 10px; overflow: auto; }
            .profile-container { display: flex; align-items: center; margin-top: 15px; }
            .profile-pic { width: 50px; height: 50px; border-radius: 50%; margin-right: 15px; }
            .profile-info { flex: 1; }
            .status { padding: 10px; margin-bottom: 15px; border-radius: 5px; }
            .status.ok { background-color: #d4edda; border: 1px solid #c3e6cb; color: #155724; }
            .status.error { background-color: #f8d7da; border: 1px solid #f5c6cb; color: #721c24; }
        </style>
    </head>
    <body>
        <h1>Instagram Data API</h1>
        
        <div id="login-status" class="status">Checking login status...</div>
        
        <div class="form-group">
            <label for="insta-url">Enter Instagram URL:</label>
            <input type="text" id="insta-url" placeholder="https://www.instagram.com/p/SHORTCODE/">
        </div>
        <div class="button-group">
            <button onclick="fetchData()">Get Full Data</button>
            <button onclick="fetchDirectData()">Get Direct Media URL</button>
        </div>
        <div id="result" class="result" style="display:none;"></div>
        
        <script>
            // Check login status on page load
            document.addEventListener('DOMContentLoaded', function() {
                fetch('/login')
                    .then(response => response.json())
                    .then(data => {
                        const statusDiv = document.getElementById('login-status');
                        if (data.status === 'authenticated') {
                            statusDiv.className = 'status ok';
                            statusDiv.innerHTML = `Authenticated as <strong>${data.username}</strong>`;
                        } else {
                            statusDiv.className = 'status error';
                            statusDiv.innerHTML = 'Not authenticated. Instagram API features may not work properly.';
                        }
                    })
                    .catch(error => {
                        const statusDiv = document.getElementById('login-status');
                        statusDiv.className = 'status error';
                        statusDiv.innerHTML = `Error checking authentication: ${error.message}`;
                    });
            });
            
            function fetchData() {
                const url = document.getElementById('insta-url').value;
                if (!url) {
                    alert('Please enter an Instagram URL');
                    return;
                }
                
                const resultDiv = document.getElementById('result');
                resultDiv.innerHTML = '<p>Loading data...</p>';
                resultDiv.style.display = 'block';
                
                fetch(`/api/data?url=${encodeURIComponent(url)}`)
                    .then(response => response.json())
                    .then(data => {
                        let profilePicHtml = '';
                        if (data.status === 'success' && data.data.owner && data.data.owner.profile_pic_url) {
                            profilePicHtml = `
                                <div class="profile-container">
                                    <img src="${data.data.owner.profile_pic_url}" class="profile-pic" alt="Profile Picture">
                                    <div class="profile-info">
                                        <strong>${data.data.owner.username}</strong>
                                        ${data.data.owner.full_name ? `<p>${data.data.owner.full_name}</p>` : ''}
                                    </div>
                                </div>
                            `;
                        }
                        
                        resultDiv.innerHTML = `
                            ${profilePicHtml}
                            <h3>API Response:</h3>
                            <pre>${JSON.stringify(data, null, 2)}</pre>
                            ${data.status === 'success' ? 
                                `<p><a href="${data.data.urls.download}" target="_blank">Download Media</a></p>` : ''}
                        `;
                    })
                    .catch(error => {
                        resultDiv.innerHTML = `<p>Error: ${error.message}</p>`;
                    });
            }
            
            function fetchDirectData() {
                const url = document.getElementById('insta-url').value;
                if (!url) {
                    alert('Please enter an Instagram URL');
                    return;
                }
                
                const resultDiv = document.getElementById('result');
                resultDiv.innerHTML = '<p>Loading data...</p>';
                resultDiv.style.display = 'block';
                
                fetch(`/api/direct-data?url=${encodeURIComponent(url)}`)
                    .then(response => response.json())
                    .then(data => {
                        let profilePicHtml = '';
                        if (data.status === 'success' && data.data.profile_pic) {
                            profilePicHtml = `
                                <div class="profile-container">
                                    <img src="${data.data.profile_pic}" class="profile-pic" alt="Profile Picture">
                                </div>
                            `;
                        }
                        
                        resultDiv.innerHTML = `
                            ${profilePicHtml}
                            <h3>API Response:</h3>
                            <pre>${JSON.stringify(data, null, 2)}</pre>
                            ${data.status === 'success' ? 
                                `<p><a href="${data.data.urls.download}" target="_blank">Download Media</a></p>
                                ${data.data.is_video ? 
                                  `<p><strong>Direct Video URL:</strong> <a href="${data.data.urls.video}" target="_blank">${data.data.urls.video}</a></p>` : ''}` : ''}
                        `;
                    })
                    .catch(error => {
                        resultDiv.innerHTML = `<p>Error: ${error.message}</p>`;
                    });
            }
        </script>
    </body>
    </html>
    '''

# Initialize: Try to login on application startup
@app.before_first_request
def initialize():
    login_to_instagram()

# For deployment
if __name__ == '__main__':
    # Get port from environment variable or use default
    port = int(os.environ.get('PORT', 8080))
    
    # Auto-login on startup
    login_success = login_to_instagram()
    if login_success:
        logger.info(f"Successfully logged in as {L.context.username}")
    
    # Use Gunicorn or another WSGI server in production
    if os.environ.get('FLASK_ENV') == 'development':
        app.run(host='0.0.0.0', port=port, debug=True)
    else:
        app.run(host='0.0.0.0', port=port)
