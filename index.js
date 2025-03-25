const express = require('express');
const FormData = require('form-data');
const cors = require('cors');

// Use global fetch if available (Node.js 18+), otherwise use node-fetch
const fetch = global.fetch || require('node-fetch');

const app = express();
const PORT = process.env.PORT || 3000;

// Middleware
app.use(express.json());
app.use(cors());

class InstagramDownloader {
    constructor() {
        this.baseUrl = 'https://snapinsta.app/get-data.php';
    }

    async downloadVideo(instagramUrl) {
        try {
            // Create form data
            const formData = new FormData();
            formData.append('url', instagramUrl);

            // Make the POST request
            const response = await fetch(this.baseUrl, {
                method: 'POST',
                headers: {
                    'Accept': '*/*',
                    'Accept-Language': 'en-US,en;q=0.7',
                    'X-Requested-With': 'XMLHttpRequest',
                    'Referer': 'https://snapinsta.app/',
                    'Origin': 'https://snapinsta.app',
                },
                body: formData
            });

            if (!response.ok) {
                throw new Error(`HTTP error! status: ${response.status}`);
            }

            const rawData = await response.text();
            // Parse the response data
            const parsedData = JSON.parse(rawData);
            
            // Extract and structure the relevant information
            const result = {
                files: parsedData.files.map(file => ({
                    type: file.__type,
                    id: file.id,
                    viewCount: file.view_count,
                    thumbnailUrl: file.thumbnail_url,
                    videoUrl: file.video_url
                })),
                userInfo: {
                    username: parsedData.user_info.username,
                    avatarUrl: parsedData.user_info.avatar_url
                }
            };

            return result;
        } catch (error) {
            console.error('Error downloading video:', error);
            throw error;
        }
    }
}

// API Routes
app.post('/api/download', async (req, res) => {
    try {
        const { url } = req.body;
        
        if (!url) {
            return res.status(400).json({ error: 'Instagram URL is required' });
        }

        const downloader = new InstagramDownloader();
        const result = await downloader.downloadVideo(url);
        
        res.json({
            success: true,
            data: {
                files: result.files,
                userInfo: result.userInfo
            }
        });
    } catch (error) {
        res.status(500).json({ 
            success: false, 
            error: error.message 
        });
    }
});

// Health check endpoint
app.get('/health', (req, res) => {
    res.json({ status: 'OK' });
});

// Start server
app.listen(PORT, () => {
    console.log(`Server is running on port ${PORT}`);
});