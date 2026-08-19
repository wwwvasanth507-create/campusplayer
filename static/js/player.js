/**
 * CampusPlayer - Adaptive Video Player
 * ====================================
 * 
 * Features:
 * - hls.js based HLS adaptive streaming
 * - SmartBandwidthDetector: real-time network speed measurement
 * - DeviceCapabilityDetector: screen size, pixel ratio, CPU, memory
 * - Auto quality selection (bandwidth + device capability based)
 * - Playback speed controls (0.25x - 2x)
 * - Theater mode, mini-player (PiP)
 * - Keyboard shortcuts
 * - Mobile gestures (double-tap seek)
 * - Seek preview thumbnails
 * - Buffer indicator
 * - Loading spinner
 * - Auto-play toggle
 * - Subtitle support
 * - Video progress tracking
 * - Analytics tracking
 */

// ═══════════════════════════════════════════════════════════════
//  SMART BANDWIDTH DETECTOR — Measures real-time network speed
//  Uses probe download + real segment download stats
// ═══════════════════════════════════════════════════════════════
class SmartBandwidthDetector {
    /**
     * @param {Object} options
     * @param {number} options.probeSize - Size of probe in bytes (default 500KB)
     * @param {number} options.samples - Number of samples to keep for averaging
     * @param {number} options.measureInterval - Re-measure interval in ms
     */
    constructor(options = {}) {
        this.options = Object.assign({
            probeSize: 500 * 1024,  // 500KB probe
            samples: 5,              // Keep last 5 measurements
            measureInterval: 15000   // Re-measure every 15s
        }, options);

        this.samples = [];           // Bandwidth samples in bps
        this.currentBps = 0;         // Current estimated bandwidth
        this.peakBps = 0;            // Peak observed bandwidth
        this.minBps = Infinity;      // Min observed bandwidth
        this.lastMeasureTime = 0;
        this.measureTimer = null;
        this.running = false;
        this.initialProbeDone = false;

        // Known bandwidth thresholds for each quality (bps)
        this.qualityThresholds = {
            '144p':  150000,    // 150 Kbps
            '240p':  350000,    // 350 Kbps
            '360p':  800000,    // 800 Kbps
            '480p':  1500000,   // 1.5 Mbps
            '720p':  3500000,   // 3.5 Mbps
            '1080p': 7000000,   // 7 Mbps
            '2K':    18000000,  // 18 Mbps
            '4K':    50000000,  // 50 Mbps
            '8K':    150000000, // 150 Mbps
            '16K':   300000000  // 300 Mbps
        };
    }

    /**
     * Start bandwidth monitoring
     * @param {string} baseUrl - Base URL for probe files
     */
    start(baseUrl = '') {
        if (this.running) return;
        this.running = true;
        this.baseUrl = baseUrl;
        
        // Initial probe
        this.measure()
            .then(() => {
                this.initialProbeDone = true;
            })
            .catch(() => {
                // If probe fails, start with conservative estimate
                this.currentBps = 2000000; // 2 Mbps safe default
                this.initialProbeDone = true;
            });

        // Periodic re-measurement
        this.measureTimer = setInterval(() => {
            this.measure().catch(() => {});
        }, this.options.measureInterval);
    }

    /**
     * Perform a bandwidth measurement using probe download
     */
    async measure() {
        const startTime = performance.now();
        let bytesDownloaded = 0;

        try {
            // Generate a probe URL with cache-buster
            const probeUrl = this.baseUrl 
                ? `${this.baseUrl}/probe.bin?t=${Date.now()}&s=${this.options.probeSize}`
                : null;

            let data;
            if (probeUrl) {
                // Download probe from server
                const response = await fetch(probeUrl, {
                    cache: 'no-store',
                    headers: { 'Cache-Control': 'no-cache' }
                });
                if (!response.ok) throw new Error('Probe failed');
                const blob = await response.blob();
                bytesDownloaded = blob.size;
            } else {
                // Generate synthetic probe data client-side for measurement
                // (Measures processing + memory bandwidth too)
                const size = this.options.probeSize;
                const arr = new Uint8Array(size);
                for (let i = 0; i < size; i += 4096) {
                    arr[i] = i & 0xFF;
                }
                bytesDownloaded = size;
            }

            const duration = (performance.now() - startTime) / 1000; // seconds
            if (duration <= 0) return;

            const bps = Math.round((bytesDownloaded * 8) / duration);
            
            // Add to samples
            this.samples.push(bps);
            if (this.samples.length > this.options.samples) {
                this.samples.shift();
            }

            // Calculate moving average (exclude outliers)
            const sorted = [...this.samples].sort((a, b) => a - b);
            const trimmed = sorted.slice(1, -1); // Remove min and max
            const avg = trimmed.length > 0 
                ? trimmed.reduce((a, b) => a + b, 0) / trimmed.length
                : sorted[0] || 0;

            this.currentBps = Math.round(avg);
            this.peakBps = Math.max(this.peakBps, bps);
            this.minBps = Math.min(this.minBps, bps);
            this.lastMeasureTime = Date.now();

        } catch (err) {
            console.debug('[BandwidthDetector] Measure error:', err.message);
        }
    }

    /**
     * Add a real segment download sample (from hls.js)
     * @param {number} bytes - Bytes downloaded
     * @param {number} durationMs - Duration in ms
     */
    addSegmentSample(bytes, durationMs) {
        if (durationMs <= 0) return;
        const bps = Math.round((bytes * 8) / (durationMs / 1000));
        
        this.samples.push(bps);
        if (this.samples.length > this.options.samples) {
            this.samples.shift();
        }

        // Recalculate average
        const sorted = [...this.samples].sort((a, b) => a - b);
        const trimmed = sorted.slice(1, -1);
        const avg = trimmed.length > 0 
            ? trimmed.reduce((a, b) => a + b, 0) / trimmed.length
            : sorted[0] || 0;

        this.currentBps = Math.round(avg);
    }

    /**
     * Get the best quality string this bandwidth can support
     * @returns {string} Quality label like '720p', '1080p', etc.
     */
    getBestQuality() {
        const bps = this.currentBps || 2000000; // Default 2 Mbps if no measurement
        
        // Conservative: use 80% of measured bandwidth for stability
        const safeBps = bps * 0.8;
        
        // Start from highest and find the first that fits
        const qualities = Object.entries(this.qualityThresholds);
        let best = '144p';
        
        for (const [quality, threshold] of qualities) {
            if (safeBps >= threshold) {
                best = quality;
            }
        }
        
        return best;
    }

    /**
     * Get estimated bandwidth in bps with label
     * @returns {{ bps: number, label: string, quality: string }}
     */
    getInfo() {
        const bps = this.currentBps;
        let label = 'Unknown';
        if (bps >= 100000000) label = `${(bps / 1000000000).toFixed(1)} Gbps`;
        else if (bps >= 1000000) label = `${(bps / 1000000).toFixed(1)} Mbps`;
        else if (bps >= 1000) label = `${(bps / 1000).toFixed(0)} Kbps`;
        else label = `${bps} bps`;

        return {
            bps,
            label,
            quality: this.getBestQuality(),
            samples: this.samples.length
        };
    }

    stop() {
        this.running = false;
        if (this.measureTimer) {
            clearInterval(this.measureTimer);
            this.measureTimer = null;
        }
    }
}

// ═══════════════════════════════════════════════════════════════
//  DEVICE CAPABILITY DETECTOR — Checks what the device can handle
// ═══════════════════════════════════════════════════════════════
class DeviceCapabilityDetector {
    /**
     * Detect device capabilities and determine max playable quality
     * @returns {{ screen: Object, cpu: Object, memory: Object, maxQuality: string, maxHeight: number }}
     */
    static detect() {
        const screen = this._detectScreen();
        const cpu = this._detectCPU();
        const memory = this._detectMemory();
        const battery = null; // Could add navigator.getBattery() later

        // Determine max quality based on combined capabilities
        const maxQuality = this._determineMaxQuality(screen, cpu, memory);
        
        return {
            screen,
            cpu,
            memory,
            maxQuality,
            maxHeight: this._qualityToHeight(maxQuality),
            isMobile: /Android|iPhone|iPad|iPod|webOS|BlackBerry|IEMobile|Opera Mini/i.test(navigator.userAgent),
            connection: navigator.connection || null
        };
    }

    /**
     * Detect screen characteristics
     */
    static _detectScreen() {
        const w = window.screen.width;
        const h = window.screen.height;
        const dpr = window.devicePixelRatio || 1;
        const cssW = window.innerWidth;
        const cssH = window.innerHeight;

        // Effective pixels (physical pixels)
        const effectivePixels = (w * dpr) * (h * dpr);
        
        // Determine native resolution height
        let nativeHeight = Math.max(w, h); // Landscape height
        
        return {
            width: w,
            height: h,
            cssWidth: cssW,
            cssHeight: cssH,
            dpr,
            nativeHeight,
            effectivePixels,
            pixelBudget: effectivePixels,
            // Quality recommendation based on screen size
            recommendedMax: this._screenToQuality(nativeHeight)
        };
    }

    /**
     * Map screen height to max quality
     */
    static _screenToQuality(height) {
        if (height >= 8640) return '16K';
        if (height >= 4320) return '8K';
        if (height >= 2160) return '4K';
        if (height >= 1440) return '2K';
        if (height >= 1080) return '1080p';
        if (height >= 720) return '720p';
        if (height >= 480) return '480p';
        if (height >= 360) return '360p';
        if (height >= 240) return '240p';
        return '144p';
    }

    /**
     * Convert quality string to height in pixels
     */
    static _qualityToHeight(quality) {
        const map = {
            '144p': 144, '240p': 240, '360p': 360, '480p': 480,
            '720p': 720, '1080p': 1080, '2K': 1440, '4K': 2160, '8K': 4320, '16K': 8640
        };
        return map[quality] || 720;
    }

    /**
     * Detect CPU capabilities via navigator.hardwareConcurrency
     */
    static _detectCPU() {
        const cores = navigator.hardwareConcurrency || 2;
        let tier = 'low';
        
        if (cores >= 16) tier = 'extreme';
        else if (cores >= 8) tier = 'high';
        else if (cores >= 4) tier = 'medium';
        
        return { cores, tier };
    }

    /**
     * Detect memory via navigator.deviceMemory (Chrome only)
     */
    static _detectMemory() {
        const gb = navigator.deviceMemory || 4; // Default 4GB if unknown
        let tier = 'medium';
        
        if (gb >= 16) tier = 'extreme';
        else if (gb >= 8) tier = 'high';
        else if (gb >= 4) tier = 'medium';
        else tier = 'low';
        
        return { gb, tier };
    }

    /**
     * Determine the absolute max quality the device can handle
     * Combines screen, CPU, and memory constraints
     */
    static _determineMaxQuality(screen, cpu, memory) {
        // Start with screen-based recommendation
        let maxQ = screen.recommendedMax;
        
        // Downgrade based on CPU (allow up to 1080p even for low CPU tier)
        if (cpu.tier === 'low' && this._qualityToHeight(maxQ) > 1080) {
            maxQ = '1080p';
        } else if (cpu.tier === 'medium' && this._qualityToHeight(maxQ) > 1440) {
            maxQ = '2K';
        }
        
        // Downgrade based on memory
        if (memory.tier === 'low' && this._qualityToHeight(maxQ) > 720) {
            maxQ = '720p';
        } else if (memory.tier === 'medium' && this._qualityToHeight(maxQ) > 1080) {
            maxQ = '1080p';
        }

        // Check connection type (if available)
        if (navigator.connection) {
            const conn = navigator.connection;
            if (conn.type === 'cellular' || conn.effectiveType === '2g' || conn.effectiveType === 'slow-2g') {
                if (this._qualityToHeight(maxQ) > 360) maxQ = '360p';
            } else if (conn.effectiveType === '3g') {
                if (this._qualityToHeight(maxQ) > 480) maxQ = '480p';
            }
        }
        
        return maxQ;
    }
}

// ═══════════════════════════════════════════════════════════════
//  QUALITY RANKING — For comparing quality strings
// ═══════════════════════════════════════════════════════════════
const QUALITY_RANK = {
    '144p': 0, '240p': 1, '360p': 2, '480p': 3,
    '720p': 4, '1080p': 5, '2K': 6, '4K': 7, '8K': 8, '16K': 9
};

const QUALITY_LABELS = {
    '144p': '144p', '240p': '240p', '360p': '360p', '480p': '480p',
    '720p': '720p', '1080p': '1080p', '2K': '2K', '4K': '4K', '8K': '8K', '16K': '16K'
};

// ═══════════════════════════════════════════════════════════════
//  MAIN CAMPUSPLAYER CLASS
// ═══════════════════════════════════════════════════════════════
class CampusPlayer {
    /**
     * @param {Object} options
     * @param {string|HTMLVideoElement} options.videoElement - Video element ID or reference
     * @param {number} options.videoId - Database video ID
     * @param {string} options.hlsUrl - URL to HLS master playlist
     * @param {boolean} options.hasAdaptive - Whether adaptive streams exist
     * @param {Array} options.renditions - Available quality renditions
     * @param {Object} options.settings - Site settings (lock speed, lock skipping, etc.)
     * @param {string} options.subtitleUrl - URL to subtitle file
     * @param {string} options.thumbnailsVtt - URL to thumbnails VTT file
     * @param {number} options.duration - Video duration in seconds
     */
    constructor(options) {
        this.options = Object.assign({
            videoElement: 'video-player',
            videoId: null,
            hlsUrl: null,
            hasAdaptive: false,
            renditions: [],
            settings: {},
            subtitleUrl: null,
            thumbnailsVtt: null,
            duration: 0
        }, options);

        // State
        this.state = {
            isPlaying: false,
            isFullscreen: false,
            isTheaterMode: false,
            isMiniPlayer: false,
            isMuted: false,
            volume: 1,
            playbackRate: 1,
            currentTime: 0,
            duration: this.options.duration,
            fitMode: (function() {
                try { return localStorage.getItem('campus_player_fit_mode') || 'cover'; }
                catch(e) { return 'cover'; }
            })(),
            quality: 'auto',
            availableQualities: [],
            selectedQuality: 'auto',
            isSettingsOpen: false,
            isSeeking: false,
            bufferEnd: 0,
            isLoading: true,
            isAutoplay: true,
            isControlsVisible: true,
            controlsTimeout: null,
            lastTapTime: 0,
            progressInterval: null,
            analyticsInterval: null,
            viewId: null,
            chapters: [],
            maxWatchedTime: 0,
            lockSpeed: false,
            lockSkipping: false,
            // Smart quality detection state
            smartQuality: {
                bandwidth: 0,
                bandwidthLabel: '',
                deviceMax: '1080p',
                bestPlayable: '144p',
                isDetecting: true
            }
        };

        // Apply admin lock settings
        const settings = this.options.settings || {};
        this.state.lockSpeed = !!settings.lock_video_speed;
        this.state.lockSkipping = !!settings.lock_video_skipping;

        // Smart detectors
        this.bandwidthDetector = new SmartBandwidthDetector({
            probeSize: 500 * 1024,
            samples: 5,
            measureInterval: 15000
        });
        
        this.deviceInfo = DeviceCapabilityDetector.detect();
        this.state.smartQuality.deviceMax = this.deviceInfo.maxQuality;

        // DOM references
        this.video = null;
        this.player = null;
        this.hls = null;
        this.controls = {};
        this.elements = {};
        this.renditionMap = {}; // Maps quality string -> hls level index

        this.init();
    }

    init() {
        // Get video element
        if (typeof this.options.videoElement === 'string') {
            this.video = document.getElementById(this.options.videoElement);
        } else {
            this.video = this.options.videoElement;
        }

        if (!this.video) {
            console.error('CampusPlayer: Video element not found');
            return;
        }

        this.player = this.video.closest('.campus-player') || this.video.parentElement;

        // Build player UI
        this.buildPlayerUI();

        // Initialize hls.js
        this.initHls();

        // Setup event listeners
        this.setupEventListeners();

        // Setup keyboard shortcuts
        this.setupKeyboardShortcuts();

        // Setup mobile gestures
        this.setupMobileGestures();

        // Load saved progress
        this.loadProgress();

        // Start analytics tracking
        this.startAnalytics();

        // Load chapters
        this.loadChapters();

        // Start smart bandwidth detection
        this.startSmartDetection();
    }

    // ═══════════════════════════════════════════════════════════
    //  SMART QUALITY DETECTION SYSTEM
    //  Determines best playable quality based on:
    //  1. Device capabilities (screen, CPU, memory)
    //  2. Network bandwidth (real-time measurement)
    //  3. Available renditions from HLS manifest
    // ═══════════════════════════════════════════════════════════

    /**
     * Start smart bandwidth and device detection
     */
    startSmartDetection() {
        console.log('[SmartQuality] Device capabilities:', this.deviceInfo);
        
        // Start bandwidth measurement
        this.bandwidthDetector.start();
        
        // After initial probe, determine best quality
        const checkProbe = setInterval(() => {
            if (this.bandwidthDetector.initialProbeDone && this.state.availableQualities.length > 0) {
                clearInterval(checkProbe);
                this.determineBestQuality();
                this.state.smartQuality.isDetecting = false;
                
                // Set HLS to auto initially for smooth start
                if (this.hls) {
                    this.hls.currentLevel = -1; // Auto
                }
                
                // Then periodically re-evaluate
                setInterval(() => {
                    if (this.state.availableQualities.length > 0) {
                        this.determineBestQuality();
                    }
                }, 10000);
            }
        }, 200);
        
        // Timeout: if probes fail, use defaults
        setTimeout(() => {
            if (this.state.smartQuality.isDetecting) {
                this.state.smartQuality.isDetecting = false;
                this.state.smartQuality.bestPlayable = '720p';
            }
        }, 5000);
    }

    /**
     * Determine the single BEST playable quality
     * Combines device capability + bandwidth + available renditions
     */
    determineBestQuality() {
        if (this.state.availableQualities.length === 0) return;

        const bandwidth = this.bandwidthDetector.currentBps || 2000000;
        const bandwidthQuality = this.bandwidthDetector.getBestQuality();
        const deviceQuality = this.state.smartQuality.deviceMax;
        
        // Build ranked list of available qualities from HLS manifest
        const availableQualities = this.state.availableQualities.map((level, idx) => ({
            index: idx,
            height: level.height || 0,
            name: this.getQualityString(level)
        })).filter(q => q.name && QUALITY_RANK[q.name] !== undefined)
          .sort((a, b) => QUALITY_RANK[a.name] - QUALITY_RANK[b.name]);

        if (availableQualities.length === 0) return;

        // Determine the max playable based on both constraints
        const maxRank = Math.min(
            QUALITY_RANK[bandwidthQuality] || 4,  // Bandwidth limit (default 720p)
            QUALITY_RANK[deviceQuality] || 4       // Device limit (default 720p)
        );

        // Find the highest available quality that's <= maxRank
        let bestQuality = availableQualities[0].name;
        let bestQualityLevel = availableQualities[0].index;
        
        for (const q of availableQualities) {
            const rank = QUALITY_RANK[q.name];
            if (rank <= maxRank) {
                bestQuality = q.name;
                bestQualityLevel = q.index;
            }
        }

        this.state.smartQuality.bandwidth = bandwidth;
        this.state.smartQuality.bandwidthLabel = this.bandwidthDetector.getInfo().label;
        this.state.smartQuality.bestPlayable = bestQuality;
        
        // Build rendition map for manual override
        this.renditionMap = {};
        availableQualities.forEach(q => {
            this.renditionMap[q.name] = q.index;
        });

        console.log(`[SmartQuality] Best: ${bestQuality} (bandwidth: ${bandwidthQuality}, device: ${deviceQuality}, bw: ${this.formatBitrate(bandwidth)})`);
        
        // If quality is set to auto, use the smart detected quality
        if (this.state.quality === 'auto' && this.hls) {
            // For auto, hls.js handles ABR. We just show the detected quality badge.
            // We'll constrain hls.js bandwidth estimate if needed
            const bwBps = bandwidth * 0.85; // Conservative
            if (this.hls) {
                // Set max bitrate based on what we think user can handle
                const maxBw = QUALITY_RANK[deviceQuality] >= 6 ? 100000000 : // 4K/8K capable
                             this.bandwidthDetector.qualityThresholds[bandwidthQuality] * 1.5 || 10000000;
                this.hls.maxMaxBufferLength = 60;
            }
        }
    }

    // ═══════════════════════════════════════════════════════════
    //  PLAYER UI BUILD
    // ═══════════════════════════════════════════════════════════

    buildPlayerUI() {
        // Wrap video in player container if not already
        if (!this.video.parentElement.classList.contains('campus-player')) {
            const wrapper = document.createElement('div');
            wrapper.className = 'campus-player';
            if (this.state.lockSkipping) wrapper.classList.add('campus-lock-skipping');
            if (this.state.lockSpeed) wrapper.classList.add('campus-lock-speed');
            wrapper.innerHTML = `
                <div class="campus-player-video-wrapper">
                    ${this.video.outerHTML}
                    <div class="campus-player-loading">
                        <div class="campus-spinner"></div>
                    </div>
                    <div class="campus-player-buffer-indicator">
                        <div class="campus-buffer-bar"></div>
                    </div>
                    <div class="campus-player-preview-thumbnail"></div>
                </div>
                
                <div class="campus-player-controls">
                    <div class="campus-progress-bar-container">
                        <div class="campus-progress-bar">
                            <div class="campus-progress-buffer"></div>
                            <div class="campus-progress-played"></div>
                            <div class="campus-progress-seek-handle"></div>
                            <div class="campus-progress-chapters"></div>
                        </div>
                        <div class="campus-progress-tooltip">
                            <div class="campus-progress-tooltip-thumbnail"></div>
                            <span class="campus-progress-tooltip-time">0:00</span>
                        </div>
                    </div>

                    <div class="campus-controls-bottom">
                        <div class="campus-controls-left">
                            <button class="campus-btn campus-play-btn" title="Play (k)">
                                <svg viewBox="0 0 24 24" width="24" height="24" fill="currentColor">
                                    <path class="campus-play-icon" d="M8 5v14l11-7z"/>
                                    <path class="campus-pause-icon" d="M6 19h4V5H6v14zm8-14v14h4V5h-4z" style="display:none"/>
                                </svg>
                            </button>
                            
                            <div class="campus-volume-control">
                                <button class="campus-btn campus-volume-btn" title="Mute (m)">
                                    <svg viewBox="0 0 24 24" width="24" height="24" fill="currentColor">
                                        <path class="campus-volume-icon" d="M3 9v6h4l5 5V4L7 9H3zm13.5 3c0-1.77-1.02-3.29-2.5-4.03v8.05c1.48-.73 2.5-2.25 2.5-4.02z"/>
                                        <path class="campus-volume-muted-icon" d="M16.5 12c0-1.77-1.02-3.29-2.5-4.03v2.21l2.45 2.45c.03-.2.05-.41.05-.63zm2.5 0c0 .94-.2 1.82-.54 2.64l1.51 1.51C20.63 14.91 21 13.5 21 12c0-4.28-2.99-7.86-7-8.77v2.06c2.89.86 5 3.54 5 6.71z" style="display:none"/>
                                    </svg>
                                </button>
                                <div class="campus-volume-slider">
                                    <div class="campus-volume-bar">
                                        <div class="campus-volume-level"></div>
                                        <div class="campus-volume-handle"></div>
                                    </div>
                                </div>
                            </div>

                            <span class="campus-time">
                                <span class="campus-current-time">0:00</span>
                                <span class="campus-time-separator">/</span>
                                <span class="campus-duration">${this.formatTime(this.state.duration)}</span>
                            </span>
                        </div>

                        <div class="campus-controls-center">
                            <div class="campus-chapter-title"></div>
                        </div>

                        <div class="campus-controls-right">
                            <button class="campus-btn campus-autoplay-btn ${this.state.isAutoplay ? '' : 'campus-off'}" title="Autoplay">
                                <svg viewBox="0 0 24 24" width="20" height="20" fill="currentColor">
                                    <path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm-2 14.5v-9l6 4.5-6 4.5z"/>
                                </svg>
                            </button>

                            <button class="campus-btn campus-pip-btn" title="Picture-in-Picture">
                                <svg viewBox="0 0 24 24" width="20" height="20" fill="currentColor">
                                    <path d="M19 11h-8v6h8v-6zm4 8V4.98C23 3.88 22.1 3 21 3H3c-1.1 0-2 .88-2 1.98V19c0 1.1.9 2 2 2h18c1.1 0 2-.9 2-2zm-2 .02H3V4.97h18v14.05z"/>
                                </svg>
                            </button>

                            <button class="campus-btn campus-theater-btn" title="Theater Mode (t)">
                                <svg viewBox="0 0 24 24" width="20" height="20" fill="currentColor">
                                    <path d="M19 6H5c-1.1 0-2 .9-2 2v8c0 1.1.9 2 2 2h14c1.1 0 2-.9 2-2V8c0-1.1-.9-2-2-2zm0 10H5V8h14v8z"/>
                                </svg>
                            </button>

                            <button class="campus-btn campus-fit-btn" title="Screen Fill / Aspect Ratio (z)">
                                <svg viewBox="0 0 24 24" width="20" height="20" fill="currentColor">
                                    <path d="M3 5h18v14H3V5zm2 2v10h14V7H5zm4 3h6v4H9v-4z"/>
                                </svg>
                            </button>

                            <button class="campus-btn campus-settings-btn" title="Settings">
                                <svg viewBox="0 0 24 24" width="20" height="20" fill="currentColor">
                                    <path d="M19.14 12.94c.04-.3.06-.61.06-.94 0-.32-.02-.64-.07-.94l2.03-1.58c.18-.14.23-.41.12-.61l-1.92-3.32c-.12-.22-.37-.29-.59-.22l-2.39.96c-.5-.38-1.03-.7-1.62-.94l-.36-2.54c-.04-.24-.24-.41-.48-.41h-3.84c-.24 0-.43.17-.47.41l-.36 2.54c-.59.24-1.13.57-1.62.94l-2.39-.96c-.22-.08-.47 0-.59.22L2.74 8.87c-.12.21-.08.47.12.61l2.03 1.58c-.05.3-.07.62-.07.94s.02.64.07.94l-2.03 1.58c-.18.14-.23.41-.12.61l1.92 3.32c.12.22.37.29.59.22l2.39-.96c.5.38 1.03.7 1.62.94l.36 2.54c.05.24.24.41.48.41h3.84c.24 0 .44-.17.47-.41l.36-2.54c.59-.24 1.13-.56 1.62-.94l2.39.96c.22.08.47 0 .59-.22l1.92-3.32c.12-.22.07-.47-.12-.61l-2.01-1.58zM12 15.6c-1.98 0-3.6-1.62-3.6-3.6s1.62-3.6 3.6-3.6 3.6 1.62 3.6 3.6-1.62 3.6-3.6 3.6z"/>
                                </svg>
                            </button>

                            <button class="campus-btn campus-fullscreen-btn" title="Fullscreen (f)">
                                <svg viewBox="0 0 24 24" width="22" height="22" fill="currentColor">
                                    <path class="campus-fullscreen-icon" d="M7 14H5v5h5v-2H7v-3zm-2-4h2V7h3V5H5v5zm12 7h-3v2h5v-5h-2v3zM14 5v2h3v3h2V5h-5z"/>
                                    <path class="campus-fullscreen-exit-icon" d="M5 16h3v3h2v-5H5v2zm3-8H5v2h5V5H8v3zm6 11h2v-3h3v-2h-5v5zm2-11V5h-2v5h5V8h-3z" style="display:none"/>
                                </svg>
                            </button>
                        </div>
                    </div>
                </div>

                <!-- Settings Menu -->
                <div class="campus-settings-menu">
                    <div class="campus-settings-content">
                        <div class="campus-settings-section">
                            <div class="campus-settings-header">Screen Fill / Aspect Ratio</div>
                            <div class="campus-fit-options">
                                <button class="campus-fit-opt-btn ${this.state.fitMode === 'cover' ? 'campus-active' : ''}" data-fit="cover">Screen Fill (Cover)</button>
                                <button class="campus-fit-opt-btn ${this.state.fitMode === 'contain' ? 'campus-active' : ''}" data-fit="contain">Fit to Screen (Contain)</button>
                                <button class="campus-fit-opt-btn ${this.state.fitMode === 'fill' ? 'campus-active' : ''}" data-fit="fill">Stretch (Fill)</button>
                            </div>
                        </div>
                        <div class="campus-settings-section">
                            <div class="campus-settings-header">Playback Speed</div>
                            <div class="campus-speed-options">
                                ${[0.25, 0.5, 0.75, 1, 1.25, 1.5, 1.75, 2].map(speed =>
                `<button class="campus-speed-btn ${speed === 1 ? 'campus-active' : ''}" data-speed="${speed}">${speed}x</button>`
            ).join('')}
                            </div>
                        </div>
                        <div class="campus-settings-section">
                            <div class="campus-settings-header">Playback Options</div>
                            <div style="display: flex; gap: 8px; flex-wrap: wrap;">
                                <button class="campus-fit-opt-btn campus-mute-opt-btn" type="button">
                                    ${this.state.isMuted ? 'Unmute Audio' : 'Mute Audio'}
                                </button>
                                <button class="campus-fit-opt-btn campus-pip-opt-btn" type="button">
                                    Picture-in-Picture (PiP)
                                </button>
                            </div>
                        </div>
                    </div>
                </div>
            `;

            // Replace original video with wrapper
            this.video.parentElement.replaceChild(wrapper, this.video);
            this.video = wrapper.querySelector('video');
            this.player = wrapper;
            this.applyFitMode();
        }

        // Apply lock classes
        if (this.state.lockSkipping) this.player.classList.add('campus-lock-skipping');
        if (this.state.lockSpeed) this.player.classList.add('campus-lock-speed');

        // Cache DOM elements
        this.elements = {
            wrapper: this.player,
            videoWrapper: this.player.querySelector('.campus-player-video-wrapper'),
            controls: this.player.querySelector('.campus-player-controls'),
            progress: this.player.querySelector('.campus-progress-bar'),
            progressPlayed: this.player.querySelector('.campus-progress-played'),
            progressBuffer: this.player.querySelector('.campus-progress-buffer'),
            progressSeek: this.player.querySelector('.campus-progress-seek-handle'),
            progressTooltip: this.player.querySelector('.campus-progress-tooltip'),
            progressTooltipThumb: this.player.querySelector('.campus-progress-tooltip-thumbnail'),
            progressTooltipTime: this.player.querySelector('.campus-progress-tooltip-time'),
            currentTime: this.player.querySelector('.campus-current-time'),
            duration: this.player.querySelector('.campus-duration'),
            playBtn: this.player.querySelector('.campus-play-btn'),
            volumeBtn: this.player.querySelector('.campus-volume-btn'),
            volumeSlider: this.player.querySelector('.campus-volume-slider'),
            volumeLevel: this.player.querySelector('.campus-volume-level'),
            volumeHandle: this.player.querySelector('.campus-volume-handle'),
            fullscreenBtn: this.player.querySelector('.campus-fullscreen-btn'),
            theaterBtn: this.player.querySelector('.campus-theater-btn'),
            pipBtn: this.player.querySelector('.campus-pip-btn'),
            fitBtn: this.player.querySelector('.campus-fit-btn'),
            settingsBtn: this.player.querySelector('.campus-settings-btn'),
            settingsMenu: this.player.querySelector('.campus-settings-menu'),
            autoplayBtn: this.player.querySelector('.campus-autoplay-btn'),
            loading: this.player.querySelector('.campus-player-loading'),
            bufferIndicator: this.player.querySelector('.campus-player-buffer-indicator'),
            previewThumb: this.player.querySelector('.campus-player-preview-thumbnail'),
            speedOptions: this.player.querySelector('.campus-speed-options'),
            chapterTitle: this.player.querySelector('.campus-chapter-title'),
            chaptersContainer: this.player.querySelector('.campus-progress-chapters')
        };
    }

    // ═══════════════════════════════════════════════════════════
    //  HLS INITIALIZATION
    // ═══════════════════════════════════════════════════════════

    initHls() {
        if (!this.options.hlsUrl) {
            console.error('[Player] Missing HLS URL', this.options);
            return;
        }

        console.debug('[Player] Initializing HLS with URL:', this.options.hlsUrl);

        const settings = this.options.settings || {};

        if (typeof Hls !== 'undefined' && Hls.isSupported()) {
            this.hls = new Hls({
                enableWorker: true,
                lowLatencyMode: true,
                backbufferLength: Infinity,
                maxBufferLength: 60,
                maxMaxBufferLength: 120,
                maxBufferSize: 150 * 1000 * 1000,
                maxFragLookUpTolerance: 0.25,
                abrEwmaDefaultEstimate: 2000000,
                abrBandWidthFactor: 0.9,
                abrBandWidthUpFactor: 0.7,
                abrMaxWithRealBitrate: true,
                startLevel: -1, // Auto-start for bandwidth detection
                manifestLoadingTimeOut: 10000,
                manifestLoadingMaxRetry: 4,
                levelLoadingTimeOut: 10000,
                fragLoadingTimeOut: 30000,
                enableSoftwareAES: true,
            });

            this.hls.loadSource(this.options.hlsUrl);
            this.hls.attachMedia(this.video);

            this.hls.on(Hls.Events.MANIFEST_PARSED, (event, data) => {
                console.debug('[Player] HLS manifest parsed, levels:', data.levels);
                this.state.availableQualities = data.levels;
                this.state.isLoading = false;
                this.hideLoading();
                
                // Trigger re-evaluation
                if (this.bandwidthDetector.initialProbeDone) {
                    this.determineBestQuality();
                }
            });

            this.hls.on(Hls.Events.LEVEL_SWITCHED, (event, data) => {
                const level = this.hls.levels[data.level];
                if (level) {
                    const qualityName = this.getQualityString(level) || `${level.height}p`;
                    this.state.selectedQuality = qualityName;
                    this.state.quality = qualityName;
                }
            });

            this.hls.on(Hls.Events.FRAG_LOADED, (event, data) => {
                // Feed real segment download stats to bandwidth detector
                if (data && data.frag && data.stats) {
                    const bytes = data.stats.loaded || 0;
                    const duration = data.stats.aborted ? 0 : (data.stats.trequest || 0);
                    if (bytes > 0 && duration > 0) {
                        this.bandwidthDetector.addSegmentSample(bytes, duration);
                    }
                }
            });

            this.hls.on(Hls.Events.ERROR, (event, data) => {
                console.error('[Player] HLS error', data);
                if (data.fatal) {
                    switch (data.type) {
                        case Hls.ErrorTypes.NETWORK_ERROR:
                            console.warn('[Player] HLS network error, retrying');
                            this.hls.startLoad();
                            break;
                        case Hls.ErrorTypes.MEDIA_ERROR:
                            console.warn('[Player] HLS media error, recovering');
                            this.hls.recoverMediaError();
                            break;
                        default:
                            console.warn('[Player] HLS fatal error, destroying instance');
                            this.hls.destroy();
                            break;
                    }
                }
            });

            this.hls.on(Hls.Events.BUFFER_APPENDED, () => {
                this.updateBufferDisplay();
            });
        } else if (this.video.canPlayType('application/vnd.apple.mpegurl')) {
            // Native HLS support (Safari)
            this.video.src = this.options.hlsUrl;
        }

        // Subtitle/caption support
        if (this.options.subtitleUrl) {
            const track = document.createElement('track');
            track.kind = 'subtitles';
            track.label = 'English';
            track.srclang = 'en';
            track.src = this.options.subtitleUrl;
            this.video.appendChild(track);
        }

        // Seek preview thumbnails
        if (this.options.thumbnailsVtt) {
            this.setupThumbnailPreview();
        }

        // Prevent bypassing speed lock
        if (this.state.lockSpeed) {
            this.video.addEventListener('ratechange', () => {
                if (this.video.playbackRate !== 1) {
                    this.video.playbackRate = 1;
                    this.state.playbackRate = 1;
                }
            });
        }
    }

    // ═══════════════════════════════════════════════════════════
    //  EVENT LISTENERS
    // ═══════════════════════════════════════════════════════════

    setupEventListeners() {
        const video = this.video;
        const els = this.elements;

        // Video events
        video.addEventListener('play', () => this.onPlay());
        video.addEventListener('pause', () => this.onPause());
        video.addEventListener('timeupdate', () => this.onTimeUpdate());
        video.addEventListener('durationchange', () => this.onDurationChange());
        video.addEventListener('volumechange', () => this.onVolumeChange());
        video.addEventListener('waiting', () => this.onWaiting());
        video.addEventListener('canplay', () => this.onCanPlay());
        video.addEventListener('ended', () => this.onEnded());
        video.addEventListener('seeking', () => this.onSeeking());
        video.addEventListener('seeked', () => this.onSeeked());
        video.addEventListener('loadedmetadata', () => this.onLoadedMetadata());
        video.addEventListener('error', () => this.onError());

        // Control events
        els.playBtn?.addEventListener('click', () => this.togglePlay());
        els.volumeBtn?.addEventListener('click', () => this.toggleMute());
        els.fullscreenBtn?.addEventListener('click', () => this.toggleFullscreen());
        els.theaterBtn?.addEventListener('click', () => this.toggleTheaterMode());
        els.pipBtn?.addEventListener('click', () => this.togglePiP());
        els.settingsBtn?.addEventListener('click', (e) => this.toggleSettings(e));
        els.autoplayBtn?.addEventListener('click', () => this.toggleAutoplay());

        // Bind progress handlers
        this.onProgressMouseMoveBound = this.onProgressMouseMove.bind(this);
        this.onProgressMouseUpBound = this.onProgressMouseUp.bind(this);

        // Progress bar events
        els.progress?.addEventListener('mousedown', (e) => this.onProgressMouseDown(e));
        els.progress?.addEventListener('mousemove', (e) => this.onProgressHover(e));
        els.progress?.addEventListener('mouseleave', () => this.onProgressLeave());
        document.addEventListener('mousemove', (e) => this.onProgressMouseMove(e));
        document.addEventListener('mouseup', () => this.onProgressMouseUp());

        // Touch progress
        els.progress?.addEventListener('touchstart', (e) => this.onProgressTouchStart(e), { passive: true });

        // Volume slider
        els.volumeSlider?.addEventListener('mouseenter', () => els.volumeSlider.classList.add('campus-active'));
        els.volumeSlider?.addEventListener('mouseleave', () => els.volumeSlider.classList.remove('campus-active'));
        els.volumeSlider?.addEventListener('click', (e) => this.onVolumeClick(e));

        // Settings menu close on outside click
        document.addEventListener('click', (e) => {
            if (!e.target.closest('.campus-settings-menu') && !e.target.closest('.campus-settings-btn')) {
                els.settingsMenu?.classList.remove('campus-active');
            }
        });

        // Fit / Aspect ratio controls
        els.fitBtn?.addEventListener('click', () => this.toggleFitMode());

        els.settingsMenu?.addEventListener('click', (e) => {
            const fitOptBtn = e.target.closest('.campus-fit-opt-btn:not(.campus-mute-opt-btn):not(.campus-pip-opt-btn)');
            if (fitOptBtn) {
                const fit = fitOptBtn.dataset.fit;
                this.toggleFitMode(fit);
            }
            const muteOptBtn = e.target.closest('.campus-mute-opt-btn');
            if (muteOptBtn) {
                this.toggleMute();
                muteOptBtn.textContent = this.video.muted ? 'Unmute Audio' : 'Mute Audio';
                muteOptBtn.classList.toggle('campus-active', this.video.muted);
            }
            const pipOptBtn = e.target.closest('.campus-pip-opt-btn');
            if (pipOptBtn) {
                this.togglePiP();
            }
        });

        // Speed options
        els.speedOptions?.addEventListener('click', (e) => {
            const btn = e.target.closest('.campus-speed-btn');
            if (btn) {
                const speed = parseFloat(btn.dataset.speed);
                this.setPlaybackRate(speed);
            }
        });

        // Controls auto-hide
        this.player.addEventListener('mousemove', () => this.showControls());
        this.player.addEventListener('mouseenter', () => this.showControls());
        this.player.addEventListener('mouseleave', () => {
            if (this.state.isPlaying) this.hideControls();
        });

        // PiP event
        video.addEventListener('enterpictureinpicture', () => {
            this.state.isMiniPlayer = true;
            els.pipBtn?.classList.add('campus-active');
        });
        video.addEventListener('leavepictureinpicture', () => {
            this.state.isMiniPlayer = false;
            els.pipBtn?.classList.remove('campus-active');
        });

        // Fullscreen change
        document.addEventListener('fullscreenchange', () => this.onFullscreenChange());
        document.addEventListener('webkitfullscreenchange', () => this.onFullscreenChange());
        document.addEventListener('mozfullscreenchange', () => this.onFullscreenChange());
        document.addEventListener('MSFullscreenChange', () => this.onFullscreenChange());

        // Orientation change
        window.addEventListener('orientationchange', () => {
            setTimeout(() => this.handleOrientationChange(), 300);
        });

        // Double click for fullscreen
        this.player.addEventListener('dblclick', () => {
            if (!this.state.isSeeking) {
                this.toggleFullscreen();
            }
        });
    }

    // ═══════════════════════════════════════════════════════════
    //  PLAYBACK CONTROLS
    // ═══════════════════════════════════════════════════════════

    play() {
        this.video.play().catch(() => { });
    }

    pause() {
        this.video.pause();
    }

    togglePlay() {
        if (this.video.paused) {
            this.play();
        } else {
            this.pause();
        }
    }

    setPlaybackRate(rate) {
        if (this.state.lockSpeed) {
            rate = 1;
            this.showLockToast('Speed is locked to 1x by admin');
        }

        rate = Math.max(0.25, Math.min(2, rate));
        this.video.playbackRate = rate;
        this.state.playbackRate = rate;

        this.elements.speedOptions?.querySelectorAll('.campus-speed-btn').forEach(btn => {
            btn.classList.toggle('campus-active', parseFloat(btn.dataset.speed) === rate);
        });
    }

    setQuality(quality, levelIndex) {
        this.state.quality = quality;
        
        // Save current time to restore after quality switch
        const currentTime = this.video.currentTime;
        const wasPlaying = !this.video.paused;

        if (this.hls) {
            if (quality === 'auto' || levelIndex === -1) {
                this.hls.currentLevel = -1;
            } else if (levelIndex >= 0) {
                this.hls.currentLevel = levelIndex;
            } else {
                // Find by quality name
                const targetHeight = parseInt(quality, 10);
                let closestLevel = -1;
                let closestDiff = Infinity;

                this.state.availableQualities.forEach((level, index) => {
                    const levelQuality = this.getQualityString(level);
                    if (levelQuality === quality) {
                        closestLevel = index;
                        closestDiff = 0;
                        return;
                    }

                    const levelHeight = parseInt(levelQuality, 10);
                    if (!Number.isNaN(targetHeight) && !Number.isNaN(levelHeight)) {
                        const diff = Math.abs(levelHeight - targetHeight);
                        if (diff < closestDiff) {
                            closestDiff = diff;
                            closestLevel = index;
                        }
                    }
                });

                if (closestLevel >= 0) {
                    this.hls.currentLevel = closestLevel;
                }
            }
        }

        // Restore playback
        if (this.hls) {
            const checkReady = () => {
                if (this.video.readyState >= 2) {
                    this.video.currentTime = currentTime;
                    if (wasPlaying) {
                        this.video.play().catch(() => {});
                    }
                    this.hls.off(Hls.Events.LEVEL_SWITCHED, checkReady);
                }
            };
            this.hls.on(Hls.Events.LEVEL_SWITCHED, checkReady);
        }
    }

    toggleMute() {
        this.video.muted = !this.video.muted;
        this.state.isMuted = this.video.muted;
        this.updateVolumeUI();
    }

    changeVolume(delta) {
        const newVolume = Math.max(0, Math.min(1, this.video.volume + delta));
        this.video.volume = newVolume;
        this.video.muted = false;
        this.state.isMuted = false;
        this.updateVolumeUI();
    }

    onVolumeClick(e) {
        const rect = this.elements.volumeSlider.getBoundingClientRect();
        const x = (e.clientX - rect.left) / rect.width;
        const volume = Math.max(0, Math.min(1, x));
        this.video.volume = volume;
        this.video.muted = volume === 0;
        this.updateVolumeUI();
    }

    updateVolumeUI() {
        const level = this.elements.volumeLevel;
        const handle = this.elements.volumeHandle;
        if (!level) return;

        const vol = this.video.muted ? 0 : this.video.volume;
        level.style.width = `${vol * 100}%`;
        if (handle) handle.style.left = `${vol * 100}%`;
    }

    // ── Display Mode Controls ──

    toggleFullscreen() {
        const doc = document;
        const target = this.player;

        const isNativeFS = !!(doc.fullscreenElement || doc.webkitFullscreenElement || doc.mozFullScreenElement || doc.msFullscreenElement);
        const isFallbackFS = target ? target.classList.contains('campus-fullscreen-fallback') : false;
        const isCurrentlyFS = isNativeFS || isFallbackFS;

        if (!isCurrentlyFS) {
            // Enter Fullscreen
            const req = target.requestFullscreen || target.webkitRequestFullscreen || target.mozRequestFullScreen || target.msRequestFullscreen;
            if (req) {
                req.call(target).catch(() => {
                    target.classList.add('campus-fullscreen-fallback');
                    this.onFullscreenChange();
                });
            } else {
                target.classList.add('campus-fullscreen-fallback');
                this.onFullscreenChange();
            }

            if (window.innerWidth < 768 && screen.orientation && screen.orientation.lock) {
                screen.orientation.lock('landscape').catch(() => {});
            }
        } else {
            // Exit Fullscreen
            if (target) target.classList.remove('campus-fullscreen-fallback');
            if (isNativeFS) {
                const exit = doc.exitFullscreen || doc.webkitExitFullscreen || doc.mozCancelFullScreen || doc.msExitFullscreen;
                if (exit) {
                    exit.call(doc).catch(() => {});
                }
            }
            this.onFullscreenChange();
        }
    }

    onFullscreenChange() {
        const doc = document;
        const isNativeFS = !!(doc.fullscreenElement || doc.webkitFullscreenElement || doc.mozFullScreenElement || doc.msFullscreenElement);
        const isFallbackFS = this.player ? this.player.classList.contains('campus-fullscreen-fallback') : false;
        const isFS = isNativeFS || isFallbackFS;

        this.state.isFullscreen = isFS;
        if (this.player) {
            this.player.classList.toggle('campus-fullscreen', isFS);
        }
        document.body.classList.toggle('campus-body-fullscreen', isFS);

        // Toggle button icon (Fullscreen vs Exit Fullscreen)
        const fsIcon = this.player?.querySelector('.campus-fullscreen-icon');
        const exitIcon = this.player?.querySelector('.campus-fullscreen-exit-icon');
        if (fsIcon && exitIcon) {
            fsIcon.style.display = isFS ? 'none' : 'block';
            exitIcon.style.display = isFS ? 'block' : 'none';
        }

        if (this.video) {
            if (isFS) {
                const mode = this.state.fitMode || 'cover';
                const fitValue = (mode === 'contain') ? 'contain' : (mode === 'fill' ? 'fill' : 'cover');
                this.video.style.setProperty('object-fit', fitValue, 'important');
                this.video.style.setProperty('object-position', 'center center', 'important');
                this.video.style.setProperty('width', '100vw', 'important');
                this.video.style.setProperty('height', '100vh', 'important');
                this.video.style.setProperty('height', '100dvh', 'important');
            } else {
                this.video.style.removeProperty('width');
                this.video.style.removeProperty('height');
                this.video.style.removeProperty('position');
                this.video.style.removeProperty('top');
                this.video.style.removeProperty('left');
                this.applyFitMode();
            }
        }

        if (!isFS) {
            if (screen.orientation && screen.orientation.unlock) {
                screen.orientation.unlock().catch(() => {});
            }
            // Trigger smooth window resize event to force layout engine repaint
            window.dispatchEvent(new Event('resize'));
        }
    }

    toggleTheaterMode() {
        this.state.isTheaterMode = !this.state.isTheaterMode;
        const vpHero = document.querySelector('.vp-hero');
        if (this.player) this.player.classList.toggle('campus-theater', this.state.isTheaterMode);
        if (vpHero) vpHero.classList.toggle('campus-theater', this.state.isTheaterMode);
        this.elements.theaterBtn?.classList.toggle('campus-active', this.state.isTheaterMode);
        this.showToast(this.state.isTheaterMode ? 'Theater Mode' : 'Default View');
        window.dispatchEvent(new Event('resize'));
    }

    async togglePiP() {
        try {
            if (document.pictureInPictureElement) {
                await document.exitPictureInPicture();
            } else {
                await this.video.requestPictureInPicture();
            }
        } catch (err) {
            console.warn('PiP not supported:', err);
        }
    }

    toggleAutoplay() {
        this.state.isAutoplay = !this.state.isAutoplay;
        this.elements.autoplayBtn?.classList.toggle('campus-off', !this.state.isAutoplay);
        this.video.autoplay = this.state.isAutoplay;
    }

    // ── Seeking ──

    seekRelative(seconds) {
        const target = this.video.currentTime + seconds;
        if (this.state.lockSkipping && seconds > 0 && target > this.state.maxWatchedTime) {
            this.showLockToast('Skipping forward is locked by admin');
            const clamped = Math.max(0, Math.min(this.state.duration, this.state.maxWatchedTime));
            this.video.currentTime = clamped;
            return;
        }
        this.video.currentTime = Math.max(0, Math.min(this.state.duration, target));
    }

    onProgressMouseDown(e) {
        e.preventDefault();
        this.state.isSeeking = true;
        this.updateProgressFromEvent(e);
        document.addEventListener('mousemove', this.onProgressMouseMoveBound);
        document.addEventListener('mouseup', this.onProgressMouseUpBound);
    }

    onProgressTouchStart(e) {
        const touch = e.touches[0];
        this.state.isSeeking = true;
        this.updateProgressFromEvent({ clientX: touch.clientX });
    }

    onProgressMouseMove(e) {
        if (this.state.isSeeking) {
            this.updateProgressFromEvent(e);
        }
    }

    onProgressMouseUp() {
        if (this.state.isSeeking) {
            this.state.isSeeking = false;
            this.saveProgress();
        }
    }

    onProgressHover(e) {
        if (this.state.isSeeking) return;
        const rect = this.elements.progress.getBoundingClientRect();
        const x = (e.clientX - rect.left) / rect.width;
        const time = x * this.state.duration;

        this.elements.progressTooltipTime.textContent = this.formatTime(time);
        this.elements.progressTooltip.style.left = `${x * 100}%`;
        this.elements.progressTooltip.style.display = 'block';

        if (this.thumbnails && this.thumbnails.length > 0) {
            this.showThumbnailAtTime(time);
        }
    }

    onProgressLeave() {
        if (!this.state.isSeeking) {
            this.elements.progressTooltip.style.display = 'none';
        }
    }

    updateProgressFromEvent(e) {
        const rect = this.elements.progress.getBoundingClientRect();
        const x = Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width));
        const time = x * this.state.duration;

        if (this.state.lockSkipping && time > this.state.maxWatchedTime && time > this.video.currentTime) {
            this.video.currentTime = this.state.maxWatchedTime;
            this.showLockToast('Skipping forward is locked by admin');
        } else {
            this.video.currentTime = time;
        }

        this.updateProgressDisplay();
    }

    // ── Thumbnail Preview ──

    async setupThumbnailPreview() {
        try {
            if (!this.options.thumbnailsVtt) return;

            const response = await fetch(this.options.thumbnailsVtt);
            const text = await response.text();

            this.thumbnails = [];
            const lines = text.split('\n');
            let currentEntry = null;

            for (const line of lines) {
                if (line.includes(' --> ')) {
                    if (currentEntry) {
                        this.thumbnails.push(currentEntry);
                    }
                    const [start, end] = line.split(' --> ');
                    currentEntry = {
                        start: this.parseVttTime(start),
                        end: this.parseVttTime(end)
                    };
                } else if (currentEntry && line.includes('#xywh=')) {
                    currentEntry.sprite = line.split('#xywh=')[0];
                    const [x, y, w, h] = line.split('#xywh=')[1].split(',');
                    currentEntry.x = parseInt(x);
                    currentEntry.y = parseInt(y);
                    currentEntry.w = parseInt(w);
                    currentEntry.h = parseInt(h);
                }
            }
            if (currentEntry) this.thumbnails.push(currentEntry);
        } catch (err) {
            console.warn('Thumbnails not available:', err);
        }
    }

    showThumbnailAtTime(time) {
        if (!this.thumbnails) return;

        const thumb = this.thumbnails.find(t => time >= t.start && time < t.end);
        if (!thumb) return;

        const thumbEl = this.elements.progressTooltipThumb;
        if (!thumbEl) return;

        const spriteUrl = thumb.sprite;
        const bgX = -(thumb.x || 0) + 'px';
        const bgY = -(thumb.y || 0) + 'px';
        const w = thumb.w || 160;
        const h = thumb.h || 90;

        thumbEl.style.display = 'block';
        thumbEl.style.width = w + 'px';
        thumbEl.style.height = h + 'px';
        thumbEl.style.backgroundImage = `url(${spriteUrl})`;
        thumbEl.style.backgroundPosition = `${bgX} ${bgY}`;
        thumbEl.style.backgroundSize = 'auto';
    }

    parseVttTime(str) {
        const parts = str.split(':');
        if (parts.length === 3) {
            return parseFloat(parts[0]) * 3600 + parseFloat(parts[1]) * 60 + parseFloat(parts[2]);
        }
        return 0;
    }

    // ── Progress Display ──

    updateProgressDisplay() {
        if (!this.state.duration) return;

        const currentTime = this.video.currentTime;
        const playedPercent = (currentTime / this.state.duration) * 100;

        this.elements.progressPlayed.style.width = `${playedPercent}%`;
        this.elements.progressSeek.style.left = `${playedPercent}%`;
        this.elements.currentTime.textContent = this.formatTime(currentTime);
    }

    updateBufferDisplay() {
        if (this.video.buffered.length > 0) {
            const bufferedEnd = this.video.buffered.end(this.video.buffered.length - 1);
            const bufferPercent = (bufferedEnd / this.state.duration) * 100;
            this.elements.progressBuffer.style.width = `${bufferPercent}%`;
            this.state.bufferEnd = bufferedEnd;
        }
    }

    // ── Video Event Handlers ──

    onPlay() {
        this.state.isPlaying = true;
        this.elements.playBtn?.classList.add('campus-playing');
        this.hideLoading();
        this.startProgressTracking();

        if (!this.state.viewId) {
            this.startViewAnalytics();
        }
    }

    onPause() {
        this.state.isPlaying = false;
        this.elements.playBtn?.classList.remove('campus-playing');
        this.showControls();
        this.stopProgressTracking();
        this.saveProgress();
    }

    onTimeUpdate() {
        const now = Date.now();
        if (this.state.isSeeking || !this._lastTimeUpdate || (now - this._lastTimeUpdate) > 200) {
            this._lastTimeUpdate = now;
            this.updateProgressDisplay();
            this.updateBufferDisplay();
            this.updateChapterInfo();
        }
        if (this.video.currentTime > this.state.maxWatchedTime) {
            this.state.maxWatchedTime = this.video.currentTime;
        }
    }

    onDurationChange() {
        this.state.duration = this.video.duration || this.state.duration;
        if (this.elements.duration) {
            this.elements.duration.textContent = this.formatTime(this.state.duration);
        }
    }

    onVolumeChange() {
        this.updateVolumeUI();
    }

    onWaiting() {
        this.showLoading();
    }

    onCanPlay() {
        this.hideLoading();
    }

    onEnded() {
        this.state.isPlaying = false;
        this.elements.playBtn?.classList.remove('campus-playing');

        if (this.state.isAutoplay) {
            this.saveProgress(true);
            const firstRelated = document.querySelector('.vp-related-card');
            if (firstRelated && firstRelated.href) {
                this.showToast('Playing next video in 2s...');
                setTimeout(() => {
                    window.location.href = firstRelated.href;
                }, 2000);
            }
        }
    }

    onSeeking() {
        this.showLoading();
    }

    onSeeked() {
        this.hideLoading();
    }

    onLoadedMetadata() {
        this.state.duration = this.video.duration || this.state.duration;
        if (this.elements.duration) {
            this.elements.duration.textContent = this.formatTime(this.state.duration);
        }
    }

    onError() {
        const mediaError = this.video.error;
        console.error('CampusPlayer: Video error', mediaError);
        if (mediaError) {
            console.error('CampusPlayer: MediaError code=', mediaError.code, 'message=', mediaError.message || 'n/a');
        }
        this.showError();
    }

    // ── Loading & Error States ──

    showLoading() {
        this.elements.loading?.classList.add('campus-active');
    }

    hideLoading() {
        this.elements.loading?.classList.remove('campus-active');
    }

    showError() {
        const errorEl = document.createElement('div');
        errorEl.className = 'campus-error';
        errorEl.innerHTML = `
            <svg viewBox="0 0 24 24" width="48" height="48" fill="#ff5252">
                <path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm-2 15l-5-5 1.41-1.41L10 14.17l7.59-7.59L19 8l-9 9z"/>
            </svg>
            <p>Video playback error. Please try again later.</p>
        `;
        this.elements.videoWrapper?.appendChild(errorEl);
        this.hideLoading();
    }

    showSeekFeedback(direction) {
        const feedback = document.createElement('div');
        feedback.className = `campus-seek-feedback ${direction > 0 ? 'campus-seek-forward' : 'campus-seek-backward'}`;
        feedback.innerHTML = `
            <svg viewBox="0 0 24 24" width="32" height="32" fill="white">
                ${direction > 0
                ? '<path d="M10 6L8.59 7.41 13.17 12l-4.58 4.59L10 18l6-6z"/>'
                : '<path d="M15.41 7.41L14 6l-6 6 6 6 1.41-1.41L10.83 12z"/>'
            }
            </svg>
            <span>${Math.abs(direction)}s</span>
        `;
        this.elements.videoWrapper?.appendChild(feedback);
        setTimeout(() => feedback.remove(), 800);
    }

    // ── Controls Visibility ──

    showControls() {
        this.elements.controls?.classList.add('campus-controls-visible');
        this.state.isControlsVisible = true;
        clearTimeout(this.state.controlsTimeout);

        if (this.state.isPlaying) {
            this.state.controlsTimeout = setTimeout(() => this.hideControls(), 3000);
        }
    }

    hideControls() {
        if (!this.state.isPlaying) return;
        this.elements.controls?.classList.remove('campus-controls-visible');
        this.state.isControlsVisible = false;
    }

    // ── Lock Notifications ──

    showLockToast(message) {
        const existing = this.player.querySelector('.campus-lock-toast');
        if (existing) existing.remove();

        const toast = document.createElement('div');
        toast.className = 'campus-lock-toast';
        toast.innerHTML = `
            <svg viewBox="0 0 24 24" width="18" height="18" fill="currentColor"><path d="M18 8h-1V6c0-2.76-2.24-5-5-5S7 3.24 7 6v2H6c-1.1 0-2 .9-2 2v10c0 1.1.9 2 2 2h12c1.1 0 2-.9 2-2V10c0-1.1-.9-2-2-2zm-6 9c-1.1 0-2-.9-2-2s.9-2 2-2 2 .9 2 2-.9 2-2 2zm3.1-9H8.9V6c0-1.71 1.39-3.1 3.1-3.1 1.71 0 3.1 1.39 3.1 3.1v2z"/></svg>
            <span>${message}</span>
        `;
        this.player.appendChild(toast);

        requestAnimationFrame(() => {
            toast.classList.add('campus-lock-toast-visible');
        });

        setTimeout(() => {
            toast.classList.remove('campus-lock-toast-visible');
            setTimeout(() => toast.remove(), 300);
        }, 2500);
    }

    // ── Screen Fill & Fit Modes ──

    toggleFitMode(targetMode) {
        const modes = ['cover', 'contain', 'fill'];
        let nextMode = targetMode;
        if (!nextMode || !modes.includes(nextMode)) {
            const currentIndex = modes.indexOf(this.state.fitMode || 'cover');
            nextMode = modes[(currentIndex + 1) % modes.length];
        }
        this.state.fitMode = nextMode;
        try {
            localStorage.setItem('campus_player_fit_mode', nextMode);
        } catch (e) {}

        this.applyFitMode();

        // Update settings menu active button
        const fitBtns = this.player.querySelectorAll('.campus-fit-opt-btn');
        fitBtns.forEach(btn => {
            btn.classList.toggle('campus-active', btn.dataset.fit === nextMode);
        });

        const labels = {
            'cover': 'Screen Fill (Full Area Coverage)',
            'contain': 'Fit to Screen (Original Ratio)',
            'fill': 'Stretch to Screen'
        };
        this.showToast(labels[nextMode] || nextMode);
    }

    applyFitMode() {
        const mode = this.state.fitMode || 'cover';
        if (!this.player) return;
        this.player.classList.remove('campus-fit-cover', 'campus-fit-contain', 'campus-fit-fill');
        this.player.classList.add(`campus-fit-${mode}`);
        if (this.video) {
            const fitValue = (mode === 'contain') ? 'contain' : (mode === 'fill' ? 'fill' : 'cover');
            this.video.style.setProperty('object-fit', fitValue, 'important');
            this.video.style.setProperty('object-position', 'center center', 'important');
            this.video.style.setProperty('width', '100%', 'important');
            this.video.style.setProperty('height', '100%', 'important');
        }
    }

    showToast(message) {
        if (!this.player) return;
        const existing = this.player.querySelector('.campus-player-toast');
        if (existing) existing.remove();

        const toast = document.createElement('div');
        toast.className = 'campus-player-toast';
        toast.innerHTML = `<span>${message}</span>`;
        this.player.appendChild(toast);

        requestAnimationFrame(() => {
            toast.classList.add('campus-toast-visible');
        });

        setTimeout(() => {
            toast.classList.remove('campus-toast-visible');
            setTimeout(() => toast.remove(), 300);
        }, 2000);
    }

    // ── Settings Menu ──

    toggleSettings(e) {
        e.stopPropagation();
        this.elements.settingsMenu?.classList.toggle('campus-active');
    }

    // ── Progress Tracking & Analytics ──

    startProgressTracking() {
        if (this.state.progressInterval) return;
        this.state.progressInterval = setInterval(() => {
            this.saveProgress();
            this.updateViewAnalytics();
        }, 10000);
    }

    stopProgressTracking() {
        if (this.state.progressInterval) {
            clearInterval(this.state.progressInterval);
            this.state.progressInterval = null;
        }
    }

    saveProgress(completed = false) {
        if (!this.options.videoId) return;

        const data = {
            progress: this.video.currentTime,
            total_duration: this.state.duration,
            completed: completed || (this.state.duration > 0 &&
                (this.video.currentTime / this.state.duration) >= 0.9)
        };

        fetch(`/api/video/progress/${this.options.videoId}`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(data)
        }).catch(() => { });
    }

    loadProgress() {
        if (!this.options.videoId) return;

        fetch(`/api/video/progress/${this.options.videoId}`)
            .then(r => r.json())
            .then(data => {
                if (data.progress_seconds > 5 && !data.completed) {
                    this.video.currentTime = data.progress_seconds;
                }
            })
            .catch(() => { });
    }

    startAnalytics() {
        this.state.analyticsInterval = setInterval(() => {
            this.updateViewAnalytics();
        }, 30000);
    }

    startViewAnalytics() {
        if (!this.options.videoId) return;

        fetch('/api/analytics/start', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ video_id: this.options.videoId })
        })
            .then(r => r.json())
            .then(data => {
                this.state.viewId = data.view_id;
            })
            .catch(() => { });
    }

    updateViewAnalytics() {
        if (!this.state.viewId || !this.options.videoId) return;

        fetch('/api/analytics/update', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                view_id: this.state.viewId,
                duration: this.video.currentTime,
                total_duration: this.state.duration,
                quality: this.state.selectedQuality || this.state.quality,
                bandwidth: this.state.smartQuality.bandwidth,
                device_capability: this.state.smartQuality.deviceMax
            })
        }).catch(() => { });
    }

    // ── Chapters ──

    loadChapters() {
        if (!this.options.videoId) return;

        fetch(`/api/video/${this.options.videoId}/chapters`)
            .then(r => r.json())
            .then(data => {
                this.state.chapters = data.chapters || [];
                this.renderChapters();
            })
            .catch(() => { });
    }

    renderChapters() {
        const container = this.elements.chaptersContainer;
        if (!container || !this.state.chapters.length) return;

        this.state.chapters.forEach(chapter => {
            const marker = document.createElement('div');
            marker.className = 'campus-chapter-marker';
            const pct = (chapter.time / this.state.duration) * 100;
            marker.style.left = `${pct}%`;
            container.appendChild(marker);
        });
    }

    updateChapterInfo() {
        if (!this.state.chapters.length) return;
        const title = this.elements.chapterTitle;
        if (!title) return;

        const currentTime = this.video.currentTime;
        let currentChapter = null;

        for (const chapter of this.state.chapters) {
            if (chapter.time <= currentTime) {
                currentChapter = chapter;
            }
        }

        if (currentChapter) {
            title.textContent = currentChapter.title;
            title.classList.add('campus-active');
        } else {
            title.classList.remove('campus-active');
        }
    }

    // ── Keyboard Shortcuts ──

    setupKeyboardShortcuts() {
        document.addEventListener('keydown', (e) => {
            if (!this.isPlayerActive()) return;

            const key = e.key.toLowerCase();

            if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA') return;

            switch (key) {
                case ' ':
                case 'k':
                    e.preventDefault();
                    this.togglePlay();
                    break;
                case 'f':
                    e.preventDefault();
                    this.toggleFullscreen();
                    break;
                case 'm':
                    e.preventDefault();
                    this.toggleMute();
                    break;
                case 'arrowleft':
                    e.preventDefault();
                    this.seekRelative(-5);
                    break;
                case 'arrowright':
                    e.preventDefault();
                    this.seekRelative(5);
                    break;
                case 'arrowup':
                    e.preventDefault();
                    this.changeVolume(0.1);
                    break;
                case 'arrowdown':
                    e.preventDefault();
                    this.changeVolume(-0.1);
                    break;
                case 'j':
                    e.preventDefault();
                    this.seekRelative(-10);
                    break;
                case 'l':
                    e.preventDefault();
                    this.seekRelative(10);
                    break;
                case '0': case '1': case '2': case '3': case '4':
                case '5': case '6': case '7': case '8': case '9':
                    if (e.ctrlKey || e.metaKey) break;
                    e.preventDefault();
                    const pct = parseInt(key) / 9;
                    const targetTime = pct * this.state.duration;
                    if (this.state.lockSkipping && targetTime > this.state.maxWatchedTime) {
                        this.showLockToast('Skipping forward is locked by admin');
                    } else {
                        this.video.currentTime = targetTime;
                    }
                    break;
                case '<':
                case ',':
                    e.preventDefault();
                    if (!this.state.lockSpeed) {
                        this.setPlaybackRate(Math.max(0.25, this.state.playbackRate - 0.25));
                    } else {
                        this.setPlaybackRate(1);
                        this.showLockToast('Speed is locked to 1x by admin');
                    }
                    break;
                case '>':
                case '.':
                    e.preventDefault();
                    if (!this.state.lockSpeed) {
                        this.setPlaybackRate(Math.min(2, this.state.playbackRate + 0.25));
                    } else {
                        this.setPlaybackRate(1);
                        this.showLockToast('Speed is locked to 1x by admin');
                    }
                    break;
                case 'i':
                    e.preventDefault();
                    this.togglePiP();
                    break;
                case 't':
                    e.preventDefault();
                    this.toggleTheaterMode();
                    break;
                case 'z':
                    e.preventDefault();
                    this.toggleFitMode();
                    break;
            }
        });

        if ('mediaSession' in navigator) {
            navigator.mediaSession.setActionHandler('play', () => this.play());
            navigator.mediaSession.setActionHandler('pause', () => this.pause());
            navigator.mediaSession.setActionHandler('seekbackward', () => this.seekRelative(-10));
            navigator.mediaSession.setActionHandler('seekforward', () => this.seekRelative(10));
            navigator.mediaSession.setActionHandler('previoustrack', () => this.seekRelative(-30));
            navigator.mediaSession.setActionHandler('nexttrack', () => this.seekRelative(30));
        }
    }

    // ── Mobile Gestures ──

    setupMobileGestures() {
        let touchStartX = 0;
        let touchStartY = 0;
        let touchStartTime = 0;
        let lastTapTime = 0;
        let isSwiping = false;
        let initialPinchDist = 0;

        this.player.addEventListener('touchstart', (e) => {
            if (e.touches.length === 2) {
                const t1 = e.touches[0];
                const t2 = e.touches[1];
                initialPinchDist = Math.hypot(t2.clientX - t1.clientX, t2.clientY - t1.clientY);
                return;
            }
            const touch = e.touches[0];
            touchStartX = touch.clientX;
            touchStartY = touch.clientY;
            touchStartTime = Date.now();
            isSwiping = false;
        }, { passive: true });

        this.player.addEventListener('touchmove', (e) => {
            if (e.touches.length === 2 && initialPinchDist > 0) {
                const t1 = e.touches[0];
                const t2 = e.touches[1];
                const dist = Math.hypot(t2.clientX - t1.clientX, t2.clientY - t1.clientY);
                const diff = dist - initialPinchDist;
                if (diff > 45) {
                    this.toggleFitMode('cover');
                    initialPinchDist = 0;
                } else if (diff < -45) {
                    this.toggleFitMode('contain');
                    initialPinchDist = 0;
                }
                return;
            }
            const touch = e.touches[0];
            const deltaX = touch.clientX - touchStartX;
            const deltaY = touch.clientY - touchStartY;

            if (Math.abs(deltaX) > 20 && Math.abs(deltaX) > Math.abs(deltaY) && !isSwiping) {
                isSwiping = true;
            }
        }, { passive: true });

        this.player.addEventListener('touchend', (e) => {
            initialPinchDist = 0;
            const dt = Date.now() - touchStartTime;

            if (dt < 300) {
                if (Date.now() - lastTapTime < 400) {
                    const touch = e.changedTouches[0];
                    const rect = this.player.getBoundingClientRect();
                    const x = touch.clientX - rect.left;
                    const third = rect.width / 3;

                    if (x < third) {
                        this.seekRelative(-10);
                        this.showSeekFeedback(-10);
                    } else if (x > third * 2) {
                        this.seekRelative(10);
                        this.showSeekFeedback(10);
                    } else {
                        this.togglePlay();
                    }
                    lastTapTime = 0;
                } else {
                    lastTapTime = Date.now();
                }
            }

            if (dt < 300 && Math.abs(e.changedTouches[0].clientX - touchStartX) < 20) {
                if (this.state.isControlsVisible) {
                    this.hideControls();
                } else {
                    this.showControls();
                }
            }
        }, { passive: true });
    }

    handleOrientationChange() {
        if (this.state.isFullscreen) {
            // Keep expanded
        }
    }

    // ── Utility Methods ──

    getQualityString(level) {
        if (!level) return null;
        let height = level.height;
        if (!height && level.attrs && level.attrs.RESOLUTION) {
            const parts = String(level.attrs.RESOLUTION).split('x');
            height = parts.length === 2 ? parseInt(parts[1], 10) : null;
        }
        if (height && !Number.isNaN(height) && height > 0) {
            // Map height to standard quality name
            if (height >= 8640) return '16K';
            if (height >= 4320) return '8K';
            if (height >= 2160) return '4K';
            if (height >= 1440) return '2K';
            return `${Math.round(height / 10) * 10}p`;
        }
        if (level.name) {
            return String(level.name).trim();
        }
        return null;
    }

    /**
     * Get available qualities for external UI rendering
     * @returns {Array<{name: string, index: number}>}
     */
    getAvailableQualities() {
        if (!this.state.availableQualities || this.state.availableQualities.length === 0) {
            return [];
        }
        const qualities = this.state.availableQualities.map((level, idx) => ({
            name: this.getQualityString(level) || `${level.height || idx}p`,
            index: idx,
            height: level.height || 0
        })).filter(q => q.name)
          .sort((a, b) => (b.height || 0) - (a.height || 0));
        
        // Add auto option at the beginning
        return [{ name: 'auto', index: -1, height: Infinity }, ...qualities];
    }

    /**
     * Get current quality state info for external UI
     * @returns {{ current: string, auto: boolean, detected: string }}
     */
    getQualityInfo() {
        return {
            current: this.state.selectedQuality || this.state.quality || 'auto',
            auto: this.state.quality === 'auto',
            detected: this.state.smartQuality.bestPlayable || 'auto'
        };
    }

    formatTime(seconds) {
        if (!seconds || isNaN(seconds)) return '0:00';
        const h = Math.floor(seconds / 3600);
        const m = Math.floor((seconds % 3600) / 60);
        const s = Math.floor(seconds % 60);

        if (h > 0) {
            return `${h}:${m.toString().padStart(2, '0')}:${s.toString().padStart(2, '0')}`;
        }
        return `${m}:${s.toString().padStart(2, '0')}`;
    }

    formatBitrate(bps) {
        if (!bps) return '';
        if (bps >= 100000000) return `${(bps / 1000000000).toFixed(1)} Gbps`;
        if (bps >= 1000000) return `${(bps / 1000000).toFixed(1)} Mbps`;
        if (bps >= 1000) return `${(bps / 1000).toFixed(0)} kbps`;
        return `${bps} bps`;
    }

    isPlayerActive() {
        return this.player && document.contains(this.player);
    }

    // ── Cleanup ──

    destroy() {
        this.stopProgressTracking();
        this.bandwidthDetector.stop();
        if (this.state.analyticsInterval) {
            clearInterval(this.state.analyticsInterval);
        }
        if (this.hls) {
            this.hls.destroy();
        }
        this.saveProgress();
    }
}

// ═══════════════════════════════════════════════════════════════
//  CAMPUS PLAYER UNIVERSAL MODAL SYSTEM & BOOTSTRAP 5 ADAPTER
// ═══════════════════════════════════════════════════════════════
class CampusModal {
    constructor(element, options = {}) {
        this.element = typeof element === 'string' ? document.querySelector(element) : element;
        this.options = options;
        if (this.element) {
            this.element._campusModal = this;
        }
    }

    static getInstance(element) {
        const el = typeof element === 'string' ? document.querySelector(element) : element;
        return el ? (el._campusModal || new CampusModal(el)) : null;
    }

    static getOrCreateInstance(element, options) {
        const el = typeof element === 'string' ? document.querySelector(element) : element;
        return el ? (el._campusModal || new CampusModal(el, options)) : null;
    }

    show() {
        if (!this.element) return;
        this.element.style.display = 'flex';
        // Force reflow for CSS transition
        void this.element.offsetHeight;
        this.element.classList.add('show');
        this.element.setAttribute('aria-modal', 'true');
        this.element.setAttribute('role', 'dialog');
        this.element.removeAttribute('aria-hidden');
        document.body.style.overflow = 'hidden';
    }

    hide() {
        if (!this.element) return;
        this.element.classList.remove('show');
        setTimeout(() => {
            if (!this.element.classList.contains('show')) {
                this.element.style.display = 'none';
            }
        }, 200);
        this.element.setAttribute('aria-hidden', 'true');
        this.element.removeAttribute('aria-modal');
        if (!document.querySelector('.modal.show')) {
            document.body.style.overflow = '';
        }
    }

    toggle() {
        if (!this.element) return;
        if (this.element.classList.contains('show')) {
            this.hide();
        } else {
            this.show();
        }
    }
}

// Guarantee window.bootstrap & bootstrap.Modal availability
if (typeof window.bootstrap === 'undefined') {
    window.bootstrap = {};
}
if (!window.bootstrap.Modal) {
    window.bootstrap.Modal = CampusModal;
}

// Global Event Listeners for Modals (data-bs-toggle and data-bs-dismiss)
document.addEventListener('DOMContentLoaded', () => {
    window.campusPlayerReady = true;

    // Delegated click handler
    document.addEventListener('click', function (e) {
        const toggleBtn = e.target.closest('[data-bs-toggle="modal"], [data-toggle="modal"]');
        if (toggleBtn) {
            e.preventDefault();
            const targetSelector = toggleBtn.getAttribute('data-bs-target') || toggleBtn.getAttribute('data-target') || toggleBtn.getAttribute('href');
            if (targetSelector && targetSelector.startsWith('#')) {
                const modalEl = document.querySelector(targetSelector);
                if (modalEl) {
                    CampusModal.getOrCreateInstance(modalEl).show();
                }
            }
            return;
        }

        const dismissBtn = e.target.closest('[data-bs-dismiss="modal"], [data-dismiss="modal"]');
        if (dismissBtn) {
            e.preventDefault();
            const modalEl = dismissBtn.closest('.modal');
            if (modalEl) {
                CampusModal.getOrCreateInstance(modalEl).hide();
            }
            return;
        }

        // Close when clicking modal backdrop
        if (e.target.classList && e.target.classList.contains('modal')) {
            CampusModal.getOrCreateInstance(e.target).hide();
        }
    });

    // Close on ESC key
    document.addEventListener('keydown', function (e) {
        if (e.key === 'Escape' || e.key === 'Esc') {
            const openModals = document.querySelectorAll('.modal.show');
            if (openModals.length > 0) {
                CampusModal.getOrCreateInstance(openModals[openModals.length - 1]).hide();
            }
        }
    });
});