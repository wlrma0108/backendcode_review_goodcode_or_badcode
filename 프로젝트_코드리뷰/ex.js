class AuctionWebSocket {
    constructor(auctionId, options = {}) {
        this.auctionId = auctionId;
        this.ws = null;
        this.lastSequence = 0;
        this.reconnectAttempts = 0;
        this.maxReconnectAttempts = 5;
        this.reconnectDelay = 1000; // 1초
        this.messageBuffer = []; // 순서 보장을 위한 버퍼
        this.expectedSequence = 1;
        this.ackTimeout = 5000; // 5초
        this.pendingMessages = new Map(); // ACK 대기 중인 메시지
        
        // 콜백 함수들
        this.onBidUpdate = options.onBidUpdate || (() => {});
        this.onError = options.onError || ((error) => console.error(error));
        this.onConnect = options.onConnect || (() => {});
        this.onDisconnect = options.onDisconnect || (() => {});
        
        this.connect();
    }
    
    connect() {
        const wsUrl = `ws://localhost:8000/ws/auction/${this.auctionId}/?last_seq=${this.lastSequence}`;
        
        try {
            this.ws = new WebSocket(wsUrl);
            
            this.ws.onopen = () => this.handleOpen();
            this.ws.onmessage = (event) => this.handleMessage(event);
            this.ws.onclose = (event) => this.handleClose(event);
            this.ws.onerror = (error) => this.handleError(error);
            
        } catch (error) {
            console.error('WebSocket connection failed:', error);
            this.scheduleReconnect();
        }
    }
    
    handleOpen() {
        console.log('WebSocket connected');
        this.reconnectAttempts = 0;
        this.onConnect();
        
        // 디버깅: 연결 상태 확인
        console.log(`Connected with last_seq=${this.lastSequence}`);
    }
    
    handleMessage(event) {
        try {
            const data = JSON.parse(event.data);
            
            switch (data.type) {
                case 'initial_state':
                    this.handleInitialState(data);
                    break;
                
                case 'reconnect_sync':
                    this.handleReconnectSync(data);
                    break;
                
                case 'bid_update':
                    this.handleBidUpdate(data);
                    break;
                
                case 'ping':
                    this.handlePing(data);
                    break;
                
                case 'error':
                    this.handleErrorMessage(data);
                    break;
                
                case 'health_warning':
                    this.handleHealthWarning(data);
                    break;
                
                default:
                    console.warn('Unknown message type:', data.type);
            }
            
        } catch (error) {
            console.error('Message parsing error:', error);
        }
    }
    
    handleInitialState(data) {
        console.log('Initial state received:', data);
        this.lastSequence = data.sequence;
        this.expectedSequence = data.sequence + 1;
        
        // UI 업데이트
        if (data.data) {
            this.updateAuctionState(data.data);
        }
    }
    
    handleReconnectSync(data) {
        console.log(`Reconnect sync: ${data.missed_count} messages`);
        
        if (data.truncated) {
            console.warn('Message history truncated, some messages lost');
        }
        
        // 놓친 메시지들을 순서대로 처리
        data.messages.forEach(message => {
            this.processMessage(message);
        });
    }
    
    handleBidUpdate(data) {
        // 메시지 순서 확인
        if (data.sequence < this.expectedSequence) {
            // 이미 처리한 메시지, 무시
            console.log(`Duplicate message: seq=${data.sequence}`);
            return;
        }
        
        if (data.sequence === this.expectedSequence) {
            // 예상된 순서, 즉시 처리
            this.processMessage(data);
            this.expectedSequence++;
            
            // 버퍼에서 다음 메시지들 확인
            this.processBufferedMessages();
        } else {
            // 순서가 건너뛰어짐, 버퍼에 저장
            console.log(`Out of order message: expected=${this.expectedSequence}, got=${data.sequence}`);
            this.messageBuffer.push(data);
            this.messageBuffer.sort((a, b) => a.sequence - b.sequence);
            
            // 너무 오래 기다리면 동기화 요청
            setTimeout(() => {
                if (data.sequence > this.expectedSequence) {
                    this.requestSync();
                }
            }, 2000);
        }
    }
    
    processBufferedMessages() {
        // 버퍼에서 순서대로 처리 가능한 메시지들 처리
        while (this.messageBuffer.length > 0) {
            const message = this.messageBuffer[0];
            
            if (message.sequence === this.expectedSequence) {
                this.messageBuffer.shift();
                this.processMessage(message);
                this.expectedSequence++;
            } else {
                break;
            }
        }
    }
    
    processMessage(data) {
        console.log(`Processing message: seq=${data.sequence}, amount=${data.amount}`);
        
        // 시퀀스 업데이트
        this.lastSequence = data.sequence;
        
        // ACK 전송
        this.sendAck(data.sequence);
        
        // UI 업데이트
        this.onBidUpdate(data);
        
        // 실제 UI 업데이트 예시
        this.updateBidUI(data);
    }
    
    handlePing(data) {
        // Pong 응답
        this.send({
            type: 'pong',
            sequence: data.sequence
        });
        
        // 서버 시퀀스와 클라이언트 시퀀스 비교
        if (data.sequence > this.lastSequence + 10) {
            console.warn('Sequence mismatch detected, requesting sync');
            this.requestSync();
        }
    }
    
    handleErrorMessage(data) {
        console.error('Server error:', data.message);
        this.onError(data.message);
    }
    
    handleHealthWarning(data) {
        console.warn('Connection health warning:', data.message);
        // 재연결 고려
        if (confirm('Connection quality degraded. Reconnect?')) {
            this.reconnect();
        }
    }
    
    handleClose(event) {
        console.log(`WebSocket closed: code=${event.code}, reason=${event.reason}`);
        this.onDisconnect();
        
        // 정상 종료가 아니면 재연결 시도
        if (event.code !== 1000) {
            this.scheduleReconnect();
        }
    }
    
    handleError(error) {
        console.error('WebSocket error:', error);
        this.onError('Connection error');
    }
    
    scheduleReconnect() {
        if (this.reconnectAttempts >= this.maxReconnectAttempts) {
            console.error('Max reconnect attempts reached');
            this.onError('Failed to reconnect after maximum attempts');
            return;
        }
        
        this.reconnectAttempts++;
        const delay = this.reconnectDelay * Math.pow(2, this.reconnectAttempts - 1); // Exponential backoff
        
        console.log(`Reconnecting in ${delay}ms (attempt ${this.reconnectAttempts}/${this.maxReconnectAttempts})`);
        
        setTimeout(() => {
            this.connect();
        }, delay);
    }
    
    reconnect() {
        if (this.ws) {
            this.ws.close();
        }
        this.reconnectAttempts = 0;
        this.connect();
    }
    
    // 메시지 전송 메서드들
    
    send(data) {
        if (this.ws && this.ws.readyState === WebSocket.OPEN) {
            this.ws.send(JSON.stringify(data));
        } else {
            console.error('WebSocket not connected');
        }
    }
    
    sendBid(amount) {
        if (!amount || amount <= 0) {
            this.onError('Invalid bid amount');
            return;
        }
        
        this.send({
            type: 'bid',
            amount: amount
        });
    }
    
    sendAck(sequence) {
        this.send({
            type: 'ack',
            sequence: sequence
        });
    }
    
    requestSync() {
        console.log('Requesting sync from server');
        this.send({
            type: 'sync_request',
            from_sequence: this.lastSequence
        });
    }
    
    // UI 업데이트 헬퍼
    
    updateAuctionState(state) {
        // 경매 상태 전체 업데이트
        document.getElementById('current-price').textContent = state.current_price;
        document.getElementById('bid-count').textContent = state.bid_count;
        // ... 기타 UI 업데이트
    }
    
    updateBidUI(data) {
        // 입찰 업데이트
        const currentPriceEl = document.getElementById('current-price');
        const bidCountEl = document.getElementById('bid-count');
        
        // 애니메이션 효과
        currentPriceEl.classList.add('price-update');
        currentPriceEl.textContent = data.amount;
        
        bidCountEl.textContent = data.bid_count;
        
        // 입찰 히스토리 추가
        this.addBidToHistory({
            username: data.username,
            amount: data.amount,
            timestamp: data.timestamp
        });
        
        // 애니메이션 제거
        setTimeout(() => {
            currentPriceEl.classList.remove('price-update');
        }, 500);
    }
    
    addBidToHistory(bid) {
        const historyEl = document.getElementById('bid-history');
        const bidEl = document.createElement('div');
        bidEl.className = 'bid-item';
        bidEl.innerHTML = `
            <span class="bid-username">${bid.username}</span>
            <span class="bid-amount">${bid.amount}</span>
            <span class="bid-time">${new Date(bid.timestamp).toLocaleTimeString()}</span>
        `;
        historyEl.insertBefore(bidEl, historyEl.firstChild);
        
        // 최대 20개만 표시
        while (historyEl.children.length > 20) {
            historyEl.removeChild(historyEl.lastChild);
        }
    }
    
    close() {
        if (this.ws) {
            this.ws.close(1000, 'Client closed');
        }
    }
}

// 사용 예시
const auction = new AuctionWebSocket('auction-123', {
    onBidUpdate: (data) => {
        console.log('New bid:', data);
    },
    onError: (error) => {
        console.error('Auction error:', error);
        alert(`Error: ${error}`);
    },
    onConnect: () => {
        console.log('Connected to auction');
        document.getElementById('connection-status').textContent = 'Connected';
    },
    onDisconnect: () => {
        console.log('Disconnected from auction');
        document.getElementById('connection-status').textContent = 'Disconnected';
    }
});

// 입찰 버튼 이벤트
document.getElementById('bid-button').addEventListener('click', () => {
    const amount = parseInt(document.getElementById('bid-amount').value);
    auction.sendBid(amount);
});

// 페이지 언로드 시 연결 종료
window.addEventListener('beforeunload', () => {
    auction.close();
});
