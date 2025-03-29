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

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

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

# Instagram authentication function
def authenticate_instagram():
    """Try multiple methods to authenticate with Instagram"""
    # Method 1: Try to load from session file if available
    try:
        session_path = os.environ.get('INSTAGRAM_SESSION_FILE', '/tmp/instagram_session')
        if os.path.exists(session_path):
            username = os.environ.get('INSTAGRAM_USERNAME')
            if username:
                L.load_session_from_file(username, session_path)
                logger.info(f"Loaded Instagram session for {username} from file")
                return True
    except Exception as e:
        logger.warning(f"Could not load session from file: {e}")
    
    # Method 2: Try to create session from environment variables
    try:
        session_data = os.environ.get('INSTAGRAM_COOKIES')
        username = os.environ.get('INSTAGRAM_USERNAME')
        if session_data and username:
            # Create a temporary session file
            with open('/tmp/insta_session', 'w') as f:
                f.write(session_data)
            # Load the session
            L.load_session_from_file(username, '/tmp/insta_session')
            logger.info(f"Created Instagram session for {username} from environment")
            return True
    except Exception as e:
        logger.warning(f"Could not create session from environment: {e}")
    
    # Method 3: Direct login with username/password
    try:
        username = os.environ.get('INSTAGRAM_USERNAME')
        password = os.environ.get('INSTAGRAM_PASSWORD')
        if username and password:
            L.login(username, password)
            # Save session for future use
            L.save_session_to_file('/tmp/instagram_session')
            logger.info(f"Logged in to Instagram as {username}")
            return True
    except Exception as e:
        logger.error(f"Instagram login failed: {e}")
    
    logger.warning("No authentication method succeeded. API may have limited functionality.")
    return False

# Try to authenticate when the app starts
authenticate_instagram()

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
        # Check if it's an authentication error
        if "401" in str(e) and not authenticate_instagram():
            logger.error(f"Authentication error: {e}")
            return None, "Authentication failed. Please check Instagram credentials."
        
        logger.error(f"Instaloader error: {e}")
        return None, f"Instaloader error: {str(e)}"
    except Exception as e:
        logger.error(f"General error: {e}")
        return None, f"Error: {str(e)}"

def stream_media(url, content_type):
    """Stream media from URL without saving to disk."""
    try:
        # Add Instagram cookies to the request if available
        cookies = {}
        try:
            for cookie in L.context.session.cookies:
                cookies[cookie.name] = cookie.value
        except:
            pass
        
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
        }
        
        # Stream the content from the original URL
        req = requests.get(url, headers=headers, cookies=cookies, stream=True)
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
    
    # Get post data
    post_data, error = get_post_data(url)
    
    if error:
        # Check if it's a rate limit error
        if "Please wait a few minutes before you try again" in error:
            return jsonify({
                "status": "limited",
                "message": "Instagram is rate limiting our requests. Please try again in a few minutes.",
                "error": error
            }), 429
        # Check if it's an authentication error
        elif "Authentication failed" in error:
            return jsonify({
                "status": "auth_error",
                "message": "Instagram authentication failed. Please check credentials.",
                "error": error
            }), 401
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
        # Check if it's an authentication error
        if "401" in str(e):
            # Try to re-authenticate
            if authenticate_instagram():
                # Retry after authentication
                try:
                    post = instaloader.Post.from_shortcode(L.context, shortcode)
                    if post.is_video:
                        return stream_media(post.video_url, "video/mp4")
                    else:
                        return stream_media(post.url, "image/jpeg")
                except Exception as retry_error:
                    logger.error(f"Retry failed: {retry_error}")
            
            return jsonify({"error": "Authentication failed. Please check Instagram credentials."}), 401
        
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
        
    except instaloader.exceptions.InstaloaderException as e:
        # Check if it's an authentication error
        if "401" in str(e) and authenticate_instagram():
            # Try again after authentication
            try:
                return get_direct_data()
            except Exception as retry_error:
                logger.error(f"Retry after authentication failed: {retry_error}")
                
        logger.error(f"Instaloader error in direct-data: {e}")
        return jsonify({"status": "error", "error": str(e)}), 500
    except Exception as e:
        logger.error(f"Error in direct-data endpoint: {e}")
        return jsonify({"status": "error", "error": str(e)}), 500

# Add session status endpoint
@app.route('/api/auth-status', methods=['GET'])
def auth_status():
    """Check if Instagram authentication is working"""
    try:
        # Basic test: try to get profile of a public user
        test_user = "instagram"
        profile = instaloader.Profile.from_username(L.context, test_user)
        
        return jsonify({
            "status": "authenticated",
            "username": L.context.username if hasattr(L.context, 'username') else None,
            "test_profile": {
                "username": profile.username,
                "followers": profile.followers,
                "is_verified": profile.is_verified
            }
        })
    except Exception as e:
        # Try to authenticate
        auth_success = authenticate_instagram()
        
        return jsonify({
            "status": "error" if not auth_success else "re-authenticated",
            "error": str(e),
            "authenticated": auth_success
        })

# Health check endpoint
@app.route('/health', methods=['GET'])
def health_check():
    return jsonify({"status": "ok", "version": "1.2.0"})

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
            .status-bar { padding: 10px; margin-bottom: 15px; background: #f0f0f0; border-radius: 4px; }
            .status-ok { background: #e0f7e0; }
            .status-error { background: #f7e0e0; }
        </style>
    </head>
    <body>
        <h1>Instagram Data API</h1>
        <div id="auth-status" class="status-bar">Checking authentication status...</div>
        <div class="form-group">
            <label for="insta-url">Enter Instagram URL:</label>
            <input type="text" id="insta-url" placeholder="https://www.instagram.com/p/SHORTCODE/">
        </div>
        <div class="button-group">
            <button onclick="fetchData()">Get Full Data</button>
            <button onclick="fetchDirectData()">Get Direct Media URL</button>
            <button onclick="checkAuth()">Check Auth Status</button>
        </div>
        <div id="result" class="result" style="display:none;"></div>
        
        <script>
            // Check auth status on page load
            window.onload = checkAuth;
            
            function checkAuth() {
                const statusDiv = document.getElementById('auth-status');
                statusDiv.textContent = 'Checking authentication status...';
                statusDiv.className = 'status-bar';
                
                fetch('/api/auth-status')
                    .then(response => response.json())
                    .then(data => {
                        if (data.status === 'authenticated') {
                            statusDiv.textContent = `Authentication: OK (${data.username || 'Anonymous'})`;
                            statusDiv.className = 'status-bar status-ok';
                        } else {
                            statusDiv.textContent = `Authentication: Failed (${data.error})`;
                            statusDiv.className = 'status-bar status-error';
                        }
                    })
                    .catch(error => {
                        statusDiv.textContent = `Authentication status error: ${error.message}`;
                        statusDiv.className = 'status-bar status-error';
                    });
            }
            
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

# For deployment - use gunicorn in production
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    # Only use the development server in local environments
    if os.environ.get('ENVIRONMENT') == 'development':
        app.run(host='0.0.0.0', port=port)
    else:
        # This message is just a reminder - in production, use gunicorn or another WSGI server
        print("WARNING: For production use, run with gunicorn or another WSGI server.")
        app.run(host='0.0.0.0', port=port)
