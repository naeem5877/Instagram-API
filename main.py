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
from flask_cors import CORS  # Add this import

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
# Enable CORS for all routes
CORS(app)  # Add this line
# Fix for running behind a proxy
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

# Check cookie file status at startup
cookie_path = 'cookie.txt'
has_cookies = os.path.exists(cookie_path)
logger.info(f"Cookie file status: {'Found' if has_cookies else 'Not found'} at {cookie_path}")

# Function to check and fix cookie file
def check_cookie_file():
    cookie_path = 'cookie.txt'
    if os.path.exists(cookie_path):
        try:
            # Check if file is readable
            with open(cookie_path, 'r') as f:
                cookie_content = f.read()
                logger.info(f"Cookie file found with {len(cookie_content)} bytes")
                
            # Make sure permissions are correct
            os.chmod(cookie_path, 0o644)
            return True
        except Exception as e:
            logger.error(f"Error reading cookie file: {e}")
            return False
    else:
        logger.warning("Cookie file not found")
        return False

# Call this function at startup
check_cookie_file()

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
    logger.info(f"Extracted shortcode: {shortcode} from URL: {url}")
    
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
                        logger.error(f"Error in retry decorator: {e}")
                        raise
        return wrapper
    return decorator

# Alternative method to get post data without login
def get_post_data_no_login(url):
    """Get basic data about an Instagram post without requiring login."""
    try:
        # Extract shortcode from URL
        shortcode = extract_shortcode_from_url(url)
        logger.info(f"No-login method: Using shortcode {shortcode}")
        
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
        embed_url = f"https://www.instagram.com/p/{shortcode}/embed/"
        logger.info(f"Fetching embed URL: {embed_url}")
        response = requests.get(embed_url, headers=headers)
        response.raise_for_status()
        
        html_content = response.text
        logger.info(f"Received HTML content length: {len(html_content)}")
        
        # Basic data we can extract from embed page
        post_data = {
            "shortcode": shortcode,
            "urls": {}
        }
        
        # Try to determine if it's video
        is_video = 'video' in html_content.lower() and 'poster=' in html_content.lower()
        post_data["is_video"] = is_video
        logger.info(f"Detected content type: {'Video' if is_video else 'Image'}")
        
        # Try to get username
        username_match = re.search(r'@([A-Za-z0-9._]+)', html_content)
        if username_match:
            username = username_match.group(1)
            post_data["owner"] = {
                "username": username,
                "profile_url": f"https://www.instagram.com/{username}/"
            }
            logger.info(f"Extracted username: {username}")
        
        # Try to extract caption
        caption_match = re.search(r'<p>(.*?)</p>', html_content, re.DOTALL)
        if caption_match:
            caption = caption_match.group(1)
            # Clean HTML tags
            caption = re.sub(r'<.*?>', '', caption)
            post_data["caption"] = caption
            logger.info(f"Extracted caption (first 50 chars): {caption[:50]}...")
        
        # Construct URLs for our API endpoints
        base_url = request.host_url.rstrip('/')
        post_data["urls"]["embed"] = f"https://www.instagram.com/p/{shortcode}/embed/"
        post_data["urls"]["instagram"] = f"https://www.instagram.com/p/{shortcode}/"
        
        # Add download URL through our API
        post_data["urls"]["download"] = f"{base_url}/api/media/stream/{shortcode}"
        
        # Add media URL if we can find it (works better for images than videos)
        img_match = re.search(r'<img[^>]+src="([^"]+)"[^>]+class="[^"]*(?:EmbeddedMediaImage|post-media)[^"]*"', html_content)
        if img_match:
            post_data["urls"]["image"] = img_match.group(1)
            logger.info(f"Extracted image URL: {post_data['urls']['image'][:50]}...")
        
        video_match = re.search(r'<video[^>]+poster="([^"]+)"', html_content)
        if video_match:
            post_data["urls"]["thumbnail"] = video_match.group(1)
            logger.info(f"Extracted thumbnail URL: {post_data['urls']['thumbnail'][:50]}...")
        
        # If it's a video, try to extract the video URL
        if is_video:
            video_src_match = re.search(r'<video[^>]+src="([^"]+)"', html_content)
            if video_src_match:
                post_data["urls"]["video"] = video_src_match.group(1)
                logger.info(f"Extracted video URL: {post_data['urls']['video'][:50]}...")
        
        logger.info(f"No-login method complete. Found URLs: {list(post_data['urls'].keys())}")
        return post_data, None
    
    except Exception as e:
        logger.error(f"Error in no-login method: {str(e)}")
        return None, f"Error: {str(e)}"

@retry_with_backoff()
def get_post_data_ytdlp(url):
    """Get comprehensive data about an Instagram post using yt-dlp."""
    try:
        # Extract shortcode from URL
        shortcode = extract_shortcode_from_url(url)
        logger.info(f"yt-dlp method: Using shortcode {shortcode}")
        
        # Check cookie file
        cookie_path = 'cookie.txt'
        has_cookies = os.path.exists(cookie_path)
        logger.info(f"yt-dlp using cookie file: {cookie_path if has_cookies else 'No cookie file found'}")
        
        # Create yt-dlp options
        ydl_opts = {
            'quiet': False,  # Changed to False to see more logs
            'no_warnings': False,  # Changed to False to see warnings
            'extract_flat': True,
            'simulate': True,  # Don't download, just extract info
            'force_generic_extractor': False,
            'ignoreerrors': False,
            'nocheckcertificate': True,
            'socket_timeout': 30,
            'cookiefile': cookie_path if has_cookies else None,
            'verbose': True  # Added verbose logging
        }
        
        logger.info(f"yt-dlp options: {ydl_opts}")
        
        # Extract info using yt-dlp
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            logger.info(f"Extracting info for URL: {url}")
            info = ydl.extract_info(url, download=False)
            logger.info(f"Successfully extracted info type: {type(info)}")
            
        logger.info(f"yt-dlp extracted info keys: {info.keys() if isinstance(info, dict) else 'Not a dictionary'}")
        
        # If it's a playlist (carousel), get the first entry
        if info.get('_type') == 'playlist':
            entries = info.get('entries', [])
            if not entries:
                logger.warning("No entries found in carousel")
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
        
        logger.info(f"yt-dlp method complete. Found URLs: {list(post_data['urls'].keys())}")
        return post_data, None
    
    except Exception as e:
        logger.error(f"yt-dlp error: {str(e)}")
        return None, f"yt-dlp error: {str(e)}"

def extract_media_info(info):
    """Extract relevant media information from yt-dlp info dict."""
    try:
        logger.info(f"Extracting media info from info dict with keys: {info.keys() if isinstance(info, dict) else 'Not a dictionary'}")
        
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
        
        # Log the data type
        logger.info(f"Media type: {'Video' if post_data['is_video'] else 'Image'}")
        
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
                
                logger.info(f"Found {len(info['formats'])} video formats")
                
                for fmt in info['formats']:
                    if fmt.get('ext') == 'mp4' and fmt.get('height', 0) > best_quality:
                        best_quality = fmt.get('height', 0)
                        best_video = fmt
                
                if best_video:
                    post_data["urls"]["video"] = best_video.get('url')
                    logger.info(f"Selected best video quality: {best_quality}p")
                    logger.info(f"Video URL (first 50 chars): {post_data['urls']['video'][:50]}...")
                else:
                    logger.warning("No MP4 format found in formats list")
            
            # Always include the direct URL and thumbnail
            if 'url' in info:
                post_data["urls"]["video"] = info.get('url')
                logger.info(f"Using direct URL for video (first 50 chars): {post_data['urls']['video'][:50]}...")
            
            if 'thumbnail' in info:
                post_data["urls"]["thumbnail"] = info.get('thumbnail')
                logger.info(f"Thumbnail URL (first 50 chars): {post_data['urls']['thumbnail'][:50]}...")
        else:
            # Image post
            if 'url' in info:
                post_data["urls"]["image"] = info.get('url')
                logger.info(f"Image URL (first 50 chars): {post_data['urls']['image'][:50]}...")
            
            if 'thumbnail' in info:
                post_data["urls"]["thumbnail"] = info.get('thumbnail')
        
        logger.info(f"Extracted media info with URLs: {list(post_data['urls'].keys())}")
        return post_data
    except Exception as e:
        logger.error(f"Error extracting media info: {str(e)}")
        # Return at least a basic structure to avoid errors downstream
        return {
            "error": str(e),
            "urls": {},
            "is_video": False,
            "owner": {}
        }

def stream_media(url, content_type):
    """Stream media from URL without saving to disk."""
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
        }
        
        logger.info(f"Streaming media from URL: {url[:50]}... with content type: {content_type}")
        
        # Stream the content from the original URL
        req = requests.get(url, headers=headers, stream=True)
        req.raise_for_status()
        
        logger.info(f"Stream request status: {req.status_code}")
        
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
        
        logger.info(f"Stream request for shortcode: {shortcode}")
        
        cookie_path = 'cookie.txt'
        has_cookies = os.path.exists(cookie_path)
        logger.info(f"Streaming endpoint using cookie file: {cookie_path if has_cookies else 'No cookie file'}")
        
        # Use yt-dlp to get the direct URL
        ydl_opts = {
            'quiet': False,
            'no_warnings': False,
            'format': 'bestvideo+bestaudio/best',  # This will select the best quality with audio
            'merge_output_format': 'mp4',
            'simulate': True,
            'cookiefile': cookie_path if has_cookies else None,
            'verbose': True
        }
        
        logger.info(f"yt-dlp options for streaming: {ydl_opts}")
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            logger.info(f"Extracting info for streaming URL: {url}")
            info = ydl.extract_info(url, download=False)
            logger.info(f"Successfully extracted stream info type: {type(info)}")
            
            # If it's a playlist (carousel), get the first entry
            if info.get('_type') == 'playlist' and info.get('entries'):
                logger.info(f"Detected carousel with {len(info.get('entries', []))} items")
                media_info = info['entries'][0]
            else:
                media_info = info
                
            # Determine content type and URL
            is_video = media_info.get('ext') == 'mp4'
            logger.info(f"Stream content type: {'Video' if is_video else 'Image'}")
            
            if is_video:
                content_type = "video/mp4"
                # Check if there's a direct URL that already has audio and video
                if media_info.get('url') and (
                    media_info.get('acodec') != 'none' and 
                    media_info.get('vcodec') != 'none'
                ):
                    media_url = media_info.get('url')
                    logger.info("Using direct URL with audio and video for streaming")
                elif 'formats' in media_info and media_info['formats']:
                    # First try to find a format that already has both audio and video
                    best_combined_format = None
                    best_height = -1
                    
                    logger.info(f"Found {len(media_info['formats'])} formats")
                    
                    # Look for formats with both audio and video
                    for fmt in media_info['formats']:
                        if (fmt.get('ext') == 'mp4' and 
                            fmt.get('acodec') != 'none' and 
                            fmt.get('vcodec') != 'none' and 
                            fmt.get('height', 0) > best_height):
                            best_height = fmt.get('height', 0)
                            best_combined_format = fmt
                    
                    # If we found a combined format, use it
                    if best_combined_format:
                        media_url = best_combined_format.get('url')
                        logger.info(f"Selected combined format with audio+video at {best_height}p")
                    else:
                        # If no combined format, check if there's a format_id that yt-dlp has determined
                        # is the best combination of audio and video
                        if 'requested_formats' in media_info:
                            # This is yt-dlp's merged selection
                            logger.info("Using yt-dlp's requested_formats for best quality")
                            # Use the URL from the video format since that's what we want to stream
                            for fmt in media_info['requested_formats']:
                                if fmt.get('vcodec') != 'none':  # This is the video part
                                    media_url = fmt.get('url')
                                    logger.info(f"Using video URL from requested format: {fmt.get('format_id')}")
                                    break
                            else:
                                # Fallback to the default URL if we can't find the video part
                                media_url = media_info.get('url')
                                logger.info("Fallback to default URL")
                        else:
                            # Fallback to the highest quality video-only format
                            # Note: This might result in video without audio
                            best_video = None
                            best_quality = -1
                            
                            for fmt in media_info['formats']:
                                if fmt.get('ext') == 'mp4' and fmt.get('height', 0) > best_quality:
                                    best_quality = fmt.get('height', 0)
                                    best_video = fmt
                            
                            if best_video:
                                media_url = best_video.get('url')
                                logger.warning(f"Could not find format with audio. Using best video only: {best_quality}p")
                            else:
                                logger.warning("No MP4 format found. Using default URL which might not have audio")
                                media_url = media_info.get('url')
                else:
                    media_url = media_info.get('url')
                    logger.info("Using direct URL for video streaming (may not have audio)")
            else:
                content_type = "image/jpeg"
                media_url = media_info.get('url')
                logger.info("Using direct URL for image streaming")
            
            if not media_url:
                logger.error("No media URL found")
                return jsonify({"error": "No media URL found"}), 500
                
            logger.info(f"Final media URL for streaming (first 50 chars): {media_url[:50]}...")
            
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
    logger.info(f"Attempting to get data for URL: {url} using yt-dlp")
    post_data, error = get_post_data_ytdlp(url)
    
    # If we get an error, try the no-login approach
    if error:
        logger.info(f"yt-dlp extraction failed with error: {error}")
        logger.info("Trying no-login approach")
        post_data, new_error = get_post_data_no_login(url)
        if new_error:
            logger.error(f"Both extraction methods failed. Last error: {new_error}")
            return jsonify({
                "status": "error", 
                "error": new_error,
                "message": "Failed to extract data from Instagram.",
                "status_code": "error"
            }), 500
    
    # Check if post_data actually contains media URLs
    if post_data and 'urls' in post_data:
        if post_data.get('is_video') and not any(key in post_data['urls'] for key in ['video']):
            logger.warning("Missing video URL in extracted data")
            
        if not post_data.get('is_video') and not any(key in post_data['urls'] for key in ['image']):
            logger.warning("Missing image URL in extracted data")

    logger.info(f"Successfully retrieved data with URLs: {list(post_data.get('urls', {}).keys())}")
    
    # Try to add a direct download URL if it's missing
    if post_data and 'shortcode' in post_data and 'urls' in post_data:
        base_url = request.host_url.rstrip('/')
        shortcode = post_data['shortcode']
        post_data['urls']['download'] = f"{base_url}/api/media/stream/{shortcode}"
    
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
    
    logger.info(f"Direct data request for URL: {url}")
    
    # Extract shortcode
    shortcode = extract_shortcode_from_url(url)
    
    # Try different approaches to get the data
    logger.info("Trying to get data using yt-dlp for direct data endpoint")
    post_data, error = get_post_data_ytdlp(url)
    
    if error:
        logger.info(f"yt-dlp extraction failed with error: {error}")
        logger.info("Trying no-login approach for direct data endpoint")
        post_data, error = get_post_data_no_login(url)
    
    if error:
        logger.error(f"All extraction methods failed for direct data. Error: {error}")
        return jsonify({"status": "error", "error": error}), 500
    
    # Try to add a direct download URL if it's missing
    if post_data and 'urls' in post_data:
        base_url = request.host_url.rstrip('/')
        post_data['urls']['download'] = f"{base_url}/api/media/stream/{shortcode}"
    
    logger.info(f"Direct data retrieved with URLs: {list(post_data.get('urls', {}).keys())}")
    
    # Return simplified response
    return jsonify({
        "status": "success",
        "data": post_data
    })

@app.route('/api/embed/<shortcode>', methods=['GET'])
def get_embed(shortcode):
    """Return embed HTML for an Instagram post."""
    try:
        logger.info(f"Fetching embed for shortcode: {shortcode}")
        
        # Fetch the embed HTML from Instagram
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
        }
        
        embed_url = f"https://www.instagram.com/p/{shortcode}/embed/"
        logger.info(f"Fetching embed URL: {embed_url}")
        
        response = requests.get(embed_url, headers=headers)
        response.raise_for_status()
        
        logger.info(f"Embed response status: {response.status_code}")
        
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
    
    logger.info(f"Download request for URL: {url}")
    
    try:
        # Extract shortcode
        shortcode = extract_shortcode_from_url(url)
        
        cookie_path = 'cookie.txt'
        has_cookies = os.path.exists(cookie_path)
        logger.info(f"Download endpoint using cookie file: {cookie_path if has_cookies else 'No cookie file'}")
        
        # Use yt-dlp to get the direct URL with audio and video
        ydl_opts = {
            'quiet': False,
            'no_warnings': False,
            'format': 'bestvideo+bestaudio/best',  # This will prefer merged streams with audio
            'merge_output_format': 'mp4',  # Ensure we get MP4 format
            'simulate': True,  # Don't download, just extract info
            'cookiefile': cookie_path if has_cookies else None,
            'verbose': True
        }
        
        logger.info(f"yt-dlp options for download: {ydl_opts}")
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            logger.info(f"Extracting info for download URL: {url}")
            info = ydl.extract_info(url, download=False)
            logger.info(f"Successfully extracted download info type: {type(info)}")
            
            # If it's a playlist (carousel), get the first entry
            if info.get('_type') == 'playlist' and info.get('entries'):
                logger.info(f"Detected carousel with {len(info.get('entries', []))} items")
                media_info = info['entries'][0]
            else:
                media_info = info
                
            # Determine content type and URL
            is_video = media_info.get('ext') == 'mp4'
            logger.info(f"Download content type: {'Video' if is_video else 'Image'}")
            
            if is_video:
                content_type = "video/mp4"
                # Check if there's a direct URL that already has audio and video
                if media_info.get('url') and (
                    media_info.get('acodec') != 'none' and 
                    media_info.get('vcodec') != 'none'
                ):
                    media_url = media_info.get('url')
                    logger.info("Using direct URL with audio and video")
                elif 'formats' in media_info and media_info['formats']:
                    # First try to find a format that already has both audio and video
                    best_combined_format = None
                    best_height = -1
                    
                    logger.info(f"Found {len(media_info['formats'])} formats")
                    
                    # Look for formats with both audio and video
                    for fmt in media_info['formats']:
                        if (fmt.get('ext') == 'mp4' and 
                            fmt.get('acodec') != 'none' and 
                            fmt.get('vcodec') != 'none' and 
                            fmt.get('height', 0) > best_height):
                            best_height = fmt.get('height', 0)
                            best_combined_format = fmt
                    
                    # If we found a combined format, use it
                    if best_combined_format:
                        media_url = best_combined_format.get('url')
                        logger.info(f"Selected combined format with audio+video at {best_height}p")
                    else:
                        # If no combined format, check if there's a format_id that yt-dlp has determined
                        # is the best combination of audio and video
                        if 'requested_formats' in media_info:
                            # This is yt-dlp's merged selection
                            logger.info("Using yt-dlp's requested_formats for best quality")
                            # Use the URL from the video format since that's what we want to stream
                            for fmt in media_info['requested_formats']:
                                if fmt.get('vcodec') != 'none':  # This is the video part
                                    media_url = fmt.get('url')
                                    logger.info(f"Using video URL from requested format: {fmt.get('format_id')}")
                                    break
                            else:
                                # Fallback to the default URL if we can't find the video part
                                media_url = media_info.get('url')
                                logger.info("Fallback to default URL")
                        else:
                            # Fallback to the highest quality video-only format
                            # Note: This might result in video without audio
                            best_video = None
                            best_quality = -1
                            
                            for fmt in media_info['formats']:
                                if fmt.get('ext') == 'mp4' and fmt.get('height', 0) > best_quality:
                                    best_quality = fmt.get('height', 0)
                                    best_video = fmt
                            
                            if best_video:
                                media_url = best_video.get('url')
                                logger.warning(f"Could not find format with audio. Using best video only: {best_quality}p")
                            else:
                                logger.warning("No MP4 format found. Using default URL which might not have audio")
                                media_url = media_info.get('url')
                else:
                    media_url = media_info.get('url')
                    logger.info("Using direct URL for video download (may not have audio)")
            else:
                content_type = "image/jpeg"
                media_url = media_info.get('url')
                logger.info("Using direct URL for image download")
            
            if not media_url:
                logger.error("No media URL found")
                return jsonify({"error": "No media URL found"}), 500
                
            logger.info(f"Final media URL for download (first 50 chars): {media_url[:50]}...")
            
            # Stream the media
            return stream_media(media_url, content_type)
    
    except Exception as e:
        logger.error(f"Download error: {e}")
        return jsonify({"error": str(e)}), 500

# Health check endpoint
@app.route('/health', methods=['GET'])
def health_check():
    has_cookies = os.path.exists('cookie.txt')
    logger.info(f"Health check - Cookie status: {'Present' if has_cookies else 'Missing'}")
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
    logger.info(f"Index page visit - Cookie status: {'Present' if has_cookies else 'Missing'}")
    
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
