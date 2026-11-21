// UART transmitter: 8 data bits, no parity, 1 stop bit (8N1), LSB first.
// CLKS_PER_BIT = clock frequency / baud rate (e.g. 50 MHz / 115200 = 434).
module uart_tx #(
    parameter CLKS_PER_BIT = 434
) (
    input  wire       clk,
    input  wire       rst,       // active-high reset
    input  wire       tx_start,  // pulse high for one clock to send data_in
    input  wire [7:0] data_in,
    output reg        tx,        // serial line, idles high
    output reg        tx_busy,   // high while a frame is being sent
    output reg        tx_done    // one-clock pulse when the stop bit ends
);

    localparam IDLE  = 2'd0;
    localparam START = 2'd1;
    localparam DATA  = 2'd2;
    localparam STOP  = 2'd3;

    reg [1:0]  state;
    reg [15:0] clk_count;
    reg [2:0]  bit_index;
    reg [7:0]  shift_reg;

    always @(posedge clk or posedge rst) begin
        if (rst) begin
            state     <= IDLE;
            clk_count <= 16'd0;
            bit_index <= 3'd0;
            shift_reg <= 8'd0;
            tx        <= 1'b1;
            tx_busy   <= 1'b0;
            tx_done   <= 1'b0;
        end else begin
            tx_done <= 1'b0;

            case (state)
                IDLE: begin
                    tx        <= 1'b1;
                    clk_count <= 16'd0;
                    bit_index <= 3'd0;
                    if (tx_start) begin
                        shift_reg <= data_in;   // capture the byte
                        tx_busy   <= 1'b1;
                        tx        <= 1'b0;      // start bit
                        state     <= START;
                    end
                end

                START: begin
                    if (clk_count == CLKS_PER_BIT - 1) begin
                        clk_count <= 16'd0;
                        tx        <= shift_reg[0];  // first data bit (LSB)
                        state     <= DATA;
                    end else begin
                        clk_count <= clk_count + 16'd1;
                    end
                end

                DATA: begin
                    if (clk_count == CLKS_PER_BIT - 1) begin
                        clk_count <= 16'd0;
                        if (bit_index == 3'd7) begin
                            bit_index <= 3'd0;
                            tx        <= 1'b1;      // stop bit
                            state     <= STOP;
                        end else begin
                            bit_index <= bit_index + 3'd1;
                            tx        <= shift_reg[bit_index + 3'd1];
                        end
                    end else begin
                        clk_count <= clk_count + 16'd1;
                    end
                end

                STOP: begin
                    if (clk_count == CLKS_PER_BIT - 1) begin
                        clk_count <= 16'd0;
                        tx_busy   <= 1'b0;
                        tx_done   <= 1'b1;
                        state     <= IDLE;
                    end else begin
                        clk_count <= clk_count + 16'd1;
                    end
                end
            endcase
        end
    end

endmodule
