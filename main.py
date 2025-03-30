from flask import Flask, request, jsonify, Response, make_response
import os
import re
import requests
import logging
import json
import time
from urllib.parse import urlparse
from werkzeug.middleware.proxy_fix import ProxyFix
import yt_dlp

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
# Fix for running behind a proxy
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

# Function to extract shortcode from Instagram URL
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

# Retry decorator for handling rate limits
def retry_with_backoff(max_retries=3, initial_backoff=2):
    def decorator(func):
        def wrapper(*args, **kwargs):
            retries = 0
            backoff = initial_backoff
            
            while retries < max_retries:
                try:
                    return func(*args, **kwargs)
                except Exception as e:
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

# Alternative method to get post data without login
def get_post_data_no_login(url):
    """Get basic data about an Instagram post without requiring login."""
    try:
        # Extract shortcode from URL
        shortcode = extract_shortcode_from_url(url)
        logger.info(f"Extracted shortcode: {shortcode}")
        
        # Use instagram-scraper instead of full API
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
        
        return post_data, None
    
    except Exception as e:
        logger.error(f"Error fetching post data: {e}")
        return None, f"Error: {str(e)}"

@retry_with_backoff()
def get_post_data_ytdlp(url):
    """Get comprehensive data about an Instagram post using yt-dlp."""
    try:
        # Extract shortcode from URL
        shortcode = extract_shortcode_from_url(url)
        logger.info(f"Extracted shortcode: {shortcode}")
        
        # Create yt-dlp options
        ydl_opts = {
            'quiet': True,
            'no_warnings': True,
            'extract_flat': True,
            'simulate': True,  # Don't download, just extract info
            'force_generic_extractor': False,
            'ignoreerrors': False,
            'nocheckcertificate': True,
            'socket_timeout': 30,
            'cookiefile': 'cookie.txt' if os.path.exists('cookie.txt') else None,
        }
        
        # Extract info using yt-dlp
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            
        logger.info(f"Successfully extracted info for {shortcode}")
        
        # If it's a playlist (carousel), get the first entry
        if info.get('_type') == 'playlist':
            entries = info.get('entries', [])
            if not entries:
                raise Exception("No entries found in carousel")
            
            # Store carousel info
            carousel_data = {
                "is_carousel": True,
                "carousel_items": []
            }
            
            # Process each carousel item
            for entry in entries:
                carousel_item = extract_media_info(entry)
                carousel_data["carousel_items"].append(carousel_item)
            
            # Use the first item as the main post data
            post_data = extract_media_info(entries[0])
            post_data.update(carousel_data)
        else:
            # Single post
            post_data = extract_media_info(info)
            post_data["is_carousel"] = False
        
        # Add Instagram direct link
        post_data["urls"]["instagram"] = f"https://www.instagram.com/p/{shortcode}/"
        post_data["shortcode"] = shortcode
        
        # Add direct download URL through our API
        base_url = request.host_url.rstrip('/')
        post_data["urls"]["download"] = f"{base_url}/api/media/stream/{shortcode}"
        
        return post_data, None
    
    except Exception as e:
        logger.error(f"yt-dlp error: {e}")
        return None, f"yt-dlp error: {str(e)}"

def extract_media_info(info):
    """Extract relevant media information from yt-dlp info dict."""
    # Base post data
    post_data = {
        "id": info.get('id', ''),
        "title": info.get('title', ''),
        "description": info.get('description', ''),
        "is_video": info.get('ext') == 'mp4',
        "upload_date": info.get('upload_date', ''),
        "view_count": info.get('view_count', 0),
        "like_count": info.get('like_count', 0),
        "comment_count": info.get('comment_count', 0),
        "duration": info.get('duration', None) if info.get('ext') == 'mp4' else None,
        "urls": {},
        "owner": {
            "username": info.get('uploader', ''),
            "uploader_id": info.get('uploader_id', ''),
            "uploader_url": info.get('uploader_url', '')
        }
    }
    
    # Extract hashtags from description
    if post_data["description"]:
        post_data["hashtags"] = re.findall(r'#(\w+)', post_data["description"])
    else:
        post_data["hashtags"] = []
        
    # Extract mentions from description
    if post_data["description"]:
        post_data["mentions"] = re.findall(r'@(\w+)', post_data["description"])
    else:
        post_data["mentions"] = []
    
    # Add media URLs
    if post_data["is_video"]:
        if 'formats' in info and info['formats']:
            # Get the best quality video URL
            best_video = None
            best_quality = -1
            
            for fmt in info['formats']:
                if fmt.get('ext') == 'mp4' and fmt.get('height', 0) > best_quality:
                    best_quality = fmt.get('height', 0)
                    best_video = fmt
            
            if best_video:
                post_data["urls"]["video"] = best_video.get('url')
                post_data["urls"]["thumbnail"] = info.get('thumbnail')
        else:
            # Fallback to the direct URL if formats aren't available
            post_data["urls"]["video"] = info.get('url')
            post_data["urls"]["thumbnail"] = info.get('thumbnail')
    else:
        # Image post
        post_data["urls"]["image"] = info.get('url')
    
    return post_data

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
        
        # Use yt-dlp to get the direct URL
        ydl_opts = {
            'quiet': True,
            'no_warnings': True,
            'format': 'best',  # Choose best quality
            'simulate': True,  # Don't download, just extract info
            'cookiefile': 'cookie.txt' if os.path.exists('cookie.txt') else None,
        }
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            
            # If it's a playlist (carousel), get the first entry
            if info.get('_type') == 'playlist' and info.get('entries'):
                media_info = info['entries'][0]
            else:
                media_info = info
                
            # Determine content type and URL
            is_video = media_info.get('ext') == 'mp4'
            
            if is_video:
                content_type = "video/mp4"
                if 'formats' in media_info and media_info['formats']:
                    # Get the best quality video
                    best_video = None
                    best_quality = -1
                    
                    for fmt in media_info['formats']:
                        if fmt.get('ext') == 'mp4' and fmt.get('height', 0) > best_quality:
                            best_quality = fmt.get('height', 0)
                            best_video = fmt
                    
                    if best_video:
                        media_url = best_video.get('url')
                    else:
                        media_url = media_info.get('url')
                else:
                    media_url = media_info.get('url')
            else:
                content_type = "image/jpeg"
                media_url = media_info.get('url')
            
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
    
    # Try first with yt-dlp
    post_data, error = get_post_data_ytdlp(url)
    
    # If we get an error, try the no-login approach
    if error:
        logger.info("Trying no-login approach since yt-dlp extraction failed")
        post_data, new_error = get_post_data_no_login(url)
        if new_error:
            return jsonify({
                "status": "error", 
                "error": new_error,
                "message": "Failed to extract data from Instagram.",
                "status_code": "error"
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

@app.route('/api/download', methods=['GET'])
def download_media():
    """API endpoint to download media directly."""
    # Get URL from query parameter
    url = request.args.get('url')
    
    if not url:
        return jsonify({"error": "URL parameter is required"}), 400
    
    # Validate Instagram URL
    if not re.match(r'^https?://(www\.)?instagram\.com/(p|reel|tv)/[^/]+/?.*$', url):
        return jsonify({"error": "Invalid Instagram URL"}), 400
    
    try:
        # Extract shortcode
        shortcode = extract_shortcode_from_url(url)
        
        # Use yt-dlp to get the direct URL
        ydl_opts = {
            'quiet': True,
            'no_warnings': True,
            'format': 'best',  # Choose best quality
            'simulate': True,  # Don't download, just extract info
            'cookiefile': 'cookie.txt' if os.path.exists('cookie.txt') else None,
        }
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            
            # If it's a playlist (carousel), get the first entry
            if info.get('_type') == 'playlist' and info.get('entries'):
                media_info = info['entries'][0]
            else:
                media_info = info
                
            # Determine content type and URL
            is_video = media_info.get('ext') == 'mp4'
            
            if is_video:
                content_type = "video/mp4"
                if 'formats' in media_info and media_info['formats']:
                    # Get the best quality video
                    best_video = None
                    best_quality = -1
                    
                    for fmt in media_info['formats']:
                        if fmt.get('ext') == 'mp4' and fmt.get('height', 0) > best_quality:
                            best_quality = fmt.get('height', 0)
                            best_video = fmt
                    
                    if best_video:
                        media_url = best_video.get('url')
                    else:
                        media_url = media_info.get('url')
                else:
                    media_url = media_info.get('url')
            else:
                content_type = "image/jpeg"
                media_url = media_info.get('url')
            
            # Stream the media
            return stream_media(media_url, content_type)
    
    except Exception as e:
        logger.error(f"Download error: {e}")
        return jsonify({"error": str(e)}), 500

# Health check endpoint
@app.route('/health', methods=['GET'])
def health_check():
    has_cookies = os.path.exists('cookie.txt')
    return jsonify({
        "status": "ok", 
        "version": "2.0.0",
        "using_ytdlp": True,
        "cookie_file": has_cookies
    })

# Simple web interface
@app.route('/', methods=['GET'])
def index():
    has_cookies = os.path.exists('cookie.txt')
    login_status = "Using cookies for authentication" if has_cookies else "Running in login-less mode"
    
    return f'''
    <!DOCTYPE html>
    <html>
    <head>
        <title>Instagram Media Downloader (yt-dlp)</title>
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
        <h1>Instagram Media Downloader (yt-dlp)</h1>
        
        <div id="status-message" class="status info">
            Status: {login_status}
        </div>
        
        <div class="form-group">
            <label for="insta-url">Enter Instagram URL:</label>
            <input type="text" id="insta-url" placeholder="https://www.instagram.com/p/SHORTCODE/">
        </div>
        
        <div class="tabs">
            <div class="tab active" onclick="switchTab('data')">Get Data</div>
            <div class="tab" onclick="switchTab('download')">Download</div>
            <div class="tab" onclick="switchTab('embed')">View Embed</div>
        </div>
        
        <div id="data-tab">
            <div class="button-group">
                <button onclick="fetchData()">Get Full Media Data</button>
                <button onclick="fetchDirectData()">Get Basic Data</button>
            </div>
        </div>

        <div id="download-tab" style="display:none;">
            <div class="button-group">
                <button onclick="downloadMedia()">Download Media</button>
            </div>
            <p>This will download the highest quality version of the image or video.</p>
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
                document.getElementById('download-tab').style.display = 'none';
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
                        if (data.status === 'success' && data.data.owner && data.data.owner.username) {{
                            profilePicHtml = `
                                <div class="profile-container">
                                    <div class="profile-info">
                                        <strong>${{data.data.owner.username}}</strong>
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
            
            function downloadMedia() {{
                const url = document.getElementById('insta-url').value;
                if (!url) {{
                    alert('Please enter an Instagram URL');
                    return;
                }}
                
                // Redirect to the download endpoint
                window.location.href = `/api/download?url=${{encodeURIComponent(url)}}`;
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
