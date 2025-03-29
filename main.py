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
import json

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

# Load Instagram session using cookies from environment variable or file
def load_session_with_cookies():
    try:
        # Option 1: Load cookies from environment variable
        cookies_json = os.environ.get('INSTAGRAM_COOKIES')
        if cookies_json:
            # Parse the JSON string (could be array or dict)
            cookies_data = json.loads(cookies_json)
            
            # Convert array of cookie objects to a dictionary if needed
            if isinstance(cookies_data, list):
                cookies = {cookie["name"]: cookie["value"] for cookie in cookies_data}
            else:
                cookies = cookies_data  # Already a dict
            
            L.context._session.cookies.update(cookies)
            logger.info("Loaded Instagram session from cookies in environment variable")
            return True
        
        # Option 2: Load cookies from a file (if present)
        cookies_file = "instagram_cookies.json"
        if os.path.exists(cookies_file):
            with open(cookies_file, 'r') as f:
                cookies_data = json.load(f)
                
                # Convert array of cookie objects to a dictionary if needed
                if isinstance(cookies_data, list):
                    cookies = {cookie["name"]: cookie["value"] for cookie in cookies_data}
                else:
                    cookies = cookies_data  # Already a dict
                
                L.context._session.cookies.update(cookies)
            logger.info("Loaded Instagram session from cookies file")
            return True
        
        logger.warning("No Instagram cookies provided; running anonymously")
        return False
    
    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse cookies JSON: {e}")
        return False
    except Exception as e:
        logger.error(f"Failed to load cookies: {e}")
        return False

# Initialize session
load_session_with_cookies()

def extract_shortcode_from_url(url):
    """Extract the Instagram shortcode from a URL."""
    path = urlparse(url).path
    if path.endswith('/'):
        path = path[:-1]
    shortcode = path.split('/')[-1]
    return shortcode

def get_post_data(url):
    """Get comprehensive data about an Instagram post."""
    try:
        shortcode = extract_shortcode_from_url(url)
        logger.info(f"Extracted shortcode: {shortcode}")
        
        post = instaloader.Post.from_shortcode(L.context, shortcode)
        
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
        
        if post.is_video:
            post_data["urls"]["video"] = post.video_url
            post_data["urls"]["thumbnail"] = post.url
            base_url = request.host_url.rstrip('/')
            post_data["urls"]["download"] = f"{base_url}/api/media/stream/{post.shortcode}"
        else:
            post_data["urls"]["image"] = post.url
            post_data["urls"]["download"] = f"{request.host_url.rstrip('/')}/api/media/stream/{post.shortcode}"
        
        post_data["urls"]["instagram"] = f"https://www.instagram.com/p/{post.shortcode}/"
        
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
            try:
                profile = instaloader.Profile.from_username(L.context, post.owner_username)
                post_data["owner"]["profile_pic_url"] = profile.profile_pic_url
            except Exception as pic_error:
                logger.warning(f"Could not fetch profile picture: {pic_error}")
        
        return post_data, None
    
    except instaloader.exceptions.LoginRequiredException:
        return None, "Session expired or invalid cookies; login required"
    except instaloader.exceptions.ConnectionException as e:
        return None, f"Connection error, possibly rate limited: {str(e)}"
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
        req = requests.get(url, headers=headers, stream=True)
        req.raise_for_status()
        
        def generate():
            for chunk in req.iter_content(chunk_size=8192):
                yield chunk
                
        return Response(
            generate(),
            content_type=content_type,
            headers={'Content-Disposition': 'attachment'}
        )
    except Exception as e:
        logger.error(f"Streaming error: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/data', methods=['GET'])
def get_data():
    """API endpoint to get comprehensive data about an Instagram post."""
    url = request.args.get('url')
    
    if not url:
        return jsonify({"error": "URL parameter is required"}), 400
    
    if not re.match(r'^https?://(www\.)?instagram\.com/(p|reel|tv)/[^/]+/?.*$', url):
        return jsonify({"error": "Invalid Instagram URL"}), 400
    
    post_data, error = get_post_data(url)
    
    if error:
        if "Please wait a few minutes before you try again" in error:
            return jsonify({
                "status": "limited",
                "message": "Instagram is rate limiting our requests. Please try again in a few minutes.",
                "error": error
            }), 429
        return jsonify({"status": "error", "error": error}), 500
    
    return jsonify({
        "status": "success",
        "data": post_data
    })

@app.route('/api/media/stream/<shortcode>', methods=['GET'])
def stream_media_endpoint(shortcode):
    """API endpoint to stream Instagram media directly without saving to disk."""
    try:
        post = instaloader.Post.from_shortcode(L.context, shortcode)
        
        if post.is_video:
            video_url = post.video_url
            return stream_media(video_url, "video/mp4")
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
    url = request.args.get('url')
    
    if not url:
        return jsonify({"error": "URL parameter is required"}), 400
    
    if not re.match(r'^https?://(www\.)?instagram\.com/(p|reel|tv)/[^/]+/?.*$', url):
        return jsonify({"error": "Invalid Instagram URL"}), 400
    
    try:
        shortcode = extract_shortcode_from_url(url)
        logger.info(f"Extracted shortcode: {shortcode}")
        
        post = instaloader.Post.from_shortcode(L.context, shortcode)
        
        response_data = {
            "status": "success",
            "data": {
                "shortcode": post.shortcode,
                "is_video": post.is_video,
                "urls": {}
            }
        }
        
        if post.is_video:
            response_data["data"]["urls"]["video"] = post.video_url
            response_data["data"]["urls"]["thumbnail"] = post.url
        else:
            response_data["data"]["urls"]["image"] = post.url
        
        base_url = request.host_url.rstrip('/')
        response_data["data"]["urls"]["download"] = f"{base_url}/api/media/stream/{post.shortcode}"
        
        try:
            profile = instaloader.Profile.from_username(L.context, post.owner_username)
            response_data["data"]["profile_pic"] = profile.profile_pic_url
        except Exception as e:
            logger.warning(f"Could not fetch profile picture: {e}")
        
        return jsonify(response_data)
        
    except instaloader.exceptions.LoginRequiredException:
        return jsonify({"status": "error", "error": "Session expired or invalid cookies; login required"}), 401
    except instaloader.exceptions.ConnectionException as e:
        return jsonify({"status": "error", "error": f"Connection error, possibly rate limited: {str(e)}"}), 500
    except Exception as e:
        logger.error(f"Error in direct-data endpoint: {e}")
        return jsonify({"status": "error", "error": str(e)}), 500

@app.route('/health', methods=['GET'])
def health_check():
    return jsonify({"status": "ok", "version": "1.1.0"})

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
        </style>
    </head>
    <body>
        <h1>Instagram Data API</h1>
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

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port)
